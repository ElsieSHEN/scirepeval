from typing import Dict, Optional, Any

import pytorch_lightning as pl
import torch
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.loggers import TensorBoardLogger
from pytorch_lightning.utilities.types import TRAIN_DATALOADERS, EVAL_DATALOADERS, STEP_OUTPUT
import torch.nn
from torch.utils.data import DataLoader
from transformers import AutoTokenizer, AutoModel
from transformers import AdamW
from pytorch_lightning.plugins import DDPPlugin
from schedulers import InverseSquareRootSchedule, InverseSquareRootScheduleConfig
from datasets import ClassificationDataset, multi_collate, MultiLabelClassificationDataset, IRDataset, \
    CustomChainDataset, TripletDataset
from tasks import TaskFamily, load_tasks
from strategies import BatchingStrategy

pl.seed_everything(42, workers=True)


def init_weights(modules):
    for module in modules:
        module.linear.weight.data.normal_(mean=0.0, std=0.02)
        if module.linear.bias is not None:
            module.linear.bias.data.zero_()


pl_to_split_map = {"fit": "train", "validate": "dev", "test": "test", "predict": "test"}


class PhantasmLight(pl.LightningModule):
    def __init__(self, batch_size: int, use_ctrl_tokens=False, task_dict: Dict[str, TaskFamily] = None):
        super().__init__()
        self.task_dict = load_tasks() if not task_dict else task_dict
        self.heads = torch.nn.ModuleDict(
            {t.name: t.head for t in self.task_dict.values() if t.head}
        )
        init_weights(self.heads.values())
        self.multi_train = None
        self.multi_test = None
        self.multi_val = None
        self.encoder = AutoModel.from_pretrained("allenai/specter")
        self.tokenizer = AutoTokenizer.from_pretrained("allenai/specter")
        self.use_ctrl_tokens = use_ctrl_tokens
        if self.use_ctrl_tokens:
            spl_ctrl_tokens = set([t.ctrl_token for t in self.task_dict.values() if t.ctrl_token])
            if spl_ctrl_tokens:
                print("Using Control Tokens")
                special_tokens_dict = {'additional_special_tokens': list(spl_ctrl_tokens)}
                num_added_toks = self.tokenizer.add_special_tokens(special_tokens_dict)
                if num_added_toks:
                    with torch.no_grad():
                        self.encoder.resize_token_embeddings(len(self.tokenizer))
                        self.encoder.embeddings.word_embeddings.weight[-num_added_toks:,
                        :] = self.encoder.embeddings.word_embeddings.weight[self.tokenizer.cls_token_id, :]
        self.batch_size = batch_size
        self.lr = 1e-6

    def forward(self, x, token_idx=0):
        embedding = self.encoder(x)
        return embedding.last_hidden_state[:, token_idx, :]

    def configure_optimizers(self):
        """Prepare optimizer and schedule (linear warmup and decay)"""
        model = self.encoder
        no_decay = ["bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {
                "params": [p for n, p in self.named_parameters() if not any(nd in n for nd in no_decay)],
                "weight_decay": 0.0,
            },
            {
                "params": [p for n, p in self.named_parameters() if any(nd in n for nd in no_decay)],
                "weight_decay": 0.0,
            }
        ]
        optimizer = AdamW(
            optimizer_grouped_parameters, lr=self.lr, eps=1e-8
        )

        self.opt = optimizer
        scheduler_config = InverseSquareRootScheduleConfig(warmup_updates=100, warmup_init_lr=self.lr, lr=5e-5)
        scheduler = InverseSquareRootSchedule(scheduler_config, optimizer)

        return {
            "optimizer": optimizer,
            "lr_scheduler": {
                "scheduler": scheduler,
                "interval": "step",
                "frequency": 1}
        }

    def calc_loss(self, train_batch, batch_idx):
        losses, loss_per_task = [], dict()
        for name, batch in train_batch.items():
            # print(name, batch[0].shape if name not in ("s2and", "search", "specter") else batch[0][0].shape, self.global_rank)
            task = self.task_dict[name]
            idx = 0 if not task.ctrl_token else 1
            if task.type != "classification":
                query, pos, neg = batch[0][0], batch[0][1], batch[0][2]
                query_emb, pos_emb, neg_emb = self(query, idx), self(pos, idx), self(neg, idx)
                curr_loss = task.loss(query_emb, pos_emb, neg_emb)
            else:
                x, y = batch[0], batch[1]
                encoding = self(x, idx)
                logits = self.heads[name](encoding)
                curr_loss = task.loss(logits, y)
                if task.multi_label:
                    curr_loss = torch.mean(curr_loss, dim=1)
            loss_per_task[name] = torch.mean(curr_loss)
            losses.append(curr_loss)
        loss = torch.mean(torch.cat(losses))
        return loss, loss_per_task

    def training_step(self, train_batch, batch_idx):
        loss, loss_per_task = self.calc_loss(train_batch, batch_idx)
        self.log("train_loss", loss, prog_bar=True, on_step=True, on_epoch=True, batch_size=self.batch_size)
        self.log("lr", self.lr_schedulers().get_last_lr()[-1], on_step=True, on_epoch=False, prog_bar=True, logger=True)
        return {"loss": loss}

    def validation_step(self, train_batch, batch_idx) -> Optional[STEP_OUTPUT]:
        loss, loss_per_task = self.calc_loss(train_batch, batch_idx)
        for task, task_loss in loss_per_task.items():
            self.log(f"val_loss_{task}", task_loss, on_step=True, on_epoch=True, prog_bar=False,
                     batch_size=self.batch_size, rank_zero_only=True)
        self.log("val_loss", loss, on_step=True, on_epoch=False, prog_bar=True)
        self.log("avg_val_loss", loss, on_epoch=True, prog_bar=True, sync_dist=True, batch_size=self.batch_size)
        return {"val_loss": loss}

    # def validation_epoch_end(self, outputs: list) -> dict:
    #     # avg_loss = torch.stack([x['val_loss'] for x in outputs]).mean()
    #     # print(avg_loss)
    #     # avg_loss = torch.mean(self.all_gather(avg_loss))
    #     # self.log("avg_val_loss", avg_loss, on_epoch=True, prog_bar=True, rank_zero_only=True, logger=True, sync_dist=True)
    #     for task in self.task_counts:
    #         self.task_counts[task] = torch.tensor(0)

    def load_data(self, split) -> CustomChainDataset:
        dataset_list = []
        for t_name, task in self.task_dict.items():
            op_token = task.ctrl_token if self.use_ctrl_tokens else None
            if task.type == "classification":
                if task.multi_label:
                    dataset_list.append(
                        MultiLabelClassificationDataset(task_name=t_name, json_file=task.data_files[split],
                                                        tokenizer=self.tokenizer, ctrl_token=op_token,
                                                        fields=task.input_fields,
                                                        label_field=task.labels_field,
                                                        labels=task.labels,
                                                        sample_size=400000 if split == "train" else 80000))
                else:
                    dataset_list.append(ClassificationDataset(task_name=t_name, json_file=task.data_files[split],
                                                              tokenizer=self.tokenizer, ctrl_token=op_token,
                                                              fields=task.input_fields,
                                                              label_field=task.labels_field,
                                                              labels=task.labels,
                                                              sample_size=400000 if split == "train" else 80000))
            elif task.type == "ir":
                dataset_list.append(
                    IRDataset(task_name=t_name, json_file=task.data_files[split], ctrl_token=op_token,
                              tokenizer=self.tokenizer, fields=task.input_fields,
                              sample_size=400000 if split == "train" else 80000))
            else:
                dataset_list.append(
                    TripletDataset(task_name=t_name, json_file=task.data_files[split], ctrl_token=op_token,
                                   tokenizer=self.tokenizer, fields=task.input_fields,
                                   sample_size=400000 if split == "train" else 80000))
        multi_dataset = CustomChainDataset(dataset_list, batch_size=self.batch_size,
                                           device_rank=self.trainer.global_rank, num_devices=self.trainer.world_size,
                                           batching_strategy=BatchingStrategy.MIXED_PROPORTIONAL)
        if split == "train":
            self.multi_train = multi_dataset
        elif split == "dev":
            self.multi_val = multi_dataset

    def setup(self, stage: Optional[str] = None) -> None:
        self.load_data("train")

    def train_dataloader(self) -> TRAIN_DATALOADERS:
        return DataLoader(self.multi_train, batch_size=self.batch_size, collate_fn=multi_collate, num_workers=0)

    def val_dataloader(self) -> EVAL_DATALOADERS:
        self.load_data("dev")
        return DataLoader(self.multi_val, batch_size=self.batch_size, collate_fn=multi_collate, num_workers=3)

    def on_save_checkpoint(self, checkpoint: Dict[str, Any]) -> None:
        logger = self.logger
        self.encoder.save_pretrained(f'{logger.save_dir}/{logger.name}/{logger.version}/checkpoints/model/')
        self.tokenizer.save_pretrained(f'{logger.save_dir}/{logger.name}/{logger.version}/checkpoints/tokenizer/')
        self.tokenizer.save_vocabulary(f'{logger.save_dir}/{logger.name}/{logger.version}/checkpoints/tokenizer/')


if __name__ == '__main__':
    log_dir = "./lightning_logs/"
    logger = TensorBoardLogger(
        save_dir=log_dir,
        version="all",
        name='full_run'
    )

    # second part of the path shouldn't be f-string
    filepath = f'{log_dir}/{logger.name}/{logger.version}/checkpoints/'
    checkpoint_callback = ModelCheckpoint(
        dirpath=filepath,
        filename='ep-{epoch}_avg_val_loss-{avg_val_loss:.3f}',
        save_top_k=4,
        verbose=True,
        monitor='avg_val_loss',  # monitors metrics logged by self.log.
        mode='min'
    )

    model = PhantasmLight(batch_size=16, use_ctrl_tokens=False)
    trainer = pl.Trainer(logger=logger,
                         gpus=[0, 1, 2, 3],
                         strategy="ddp",
                         enable_checkpointing=True,
                         callbacks=[checkpoint_callback],
                         val_check_interval=1.0,
                         num_sanity_val_steps=4,
                         max_epochs=2,
                         precision=16,
                         accumulate_grad_batches=4)
    trainer.fit(model)