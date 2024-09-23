from KnowledgeSelection import Trainer
from utils import read_json


if __name__ == '__main__':
    data_name = "WoW"
    config = read_json(f"KnowledgeSelection/Configs/{data_name}_config.json")
    Trainer.gate_training(config)