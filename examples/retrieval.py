import sys

sys.path.append('../')

from evaluation.encoders import Model
from evaluation.evaluator import IREvaluator

model = Model(base_checkpoint="allenai/specter")
# model = Model(base_checkpoint="../lightning_logs/full_run/scincl_ctrl/checkpoints/", task_id="[SAL]",
#               use_ctrl_codes=True)
# model = Model(base_checkpoint="malteos/scincl", variant="adapters",
#               adapters_load_from="../lightning_logs/full_run/scincl_adapters/checkpoints/", task_id="[CLF]")
evaluator = IREvaluator("feeds_1", ("allenai/scirepeval", "feeds_1"), ("allenai/scirepeval_test", "feeds_1"), model,
                        metrics=("map", "ndcg",))

embeddings = evaluator.generate_embeddings()

evaluator.evaluate(embeddings)
