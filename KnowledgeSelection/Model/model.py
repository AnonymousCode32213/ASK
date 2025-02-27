import torch
import lightning.pytorch as pl
import numpy as np
from typing import List, Union

from KnowledgeSelection.Model.module import Environment, InputLayer
from KnowledgeSelection.Model.GATv2 import GATv2
from KnowledgeSelection.Model.Node import NodeSelector
from KnowledgeSelection.Model.Knowledge import KnowledgeSelector
from KnowledgeSelection.Model.model_utils import loss_function, reward_function


class KnowledgeSelectionModel(pl.LightningModule):
    def __init__(self, opt):
        super().__init__()
        torch.manual_seed(42)
        self.save_hyperparameters()

        self.opt = opt

        self.batch_size = opt["batch_size"]
        self.max_hops = opt["max_hops"]  # 2
        self.label_smoothing_rate = 0.15
        self.avg_pool_size = 0
        self.early_stop = opt["early_stop"]

        self.base_poolsize = opt["base_poolsize"]  # 40/800
        self.node_reward, self.knowledge_reward, self.pool_reward = opt["reward"]
        self.precision = opt["precision"]
        if self.precision == "16":
            self.mask = -6e4
        else:
            self.mask = -1e6

        self.environment = Environment(opt)

        self.inputlayer = InputLayer(in_features=opt["hidden_dim"], out_features=int(opt["hidden_dim"] / 2))
        self.gvt = GATv2(in_dim=opt["hidden_dim"], hidden_dim=int(opt["hidden_dim"] / 2), out_dim=1, num_heads=8, mask=self.mask)
        self.node_selector = NodeSelector(opt["hidden_dim"], self.gvt, self.mask, opt["propagation_rate"])
        self.knowledge_selector = KnowledgeSelector(opt["hidden_dim"], self.base_poolsize, opt["min_poolsize"], self.environment, self.mask)

        self.samples = opt["samples"]
        self.steps = int(1 + ((self.samples[0] / opt["rollouts"]) // self.batch_size))
        self.optimizer = torch.optim.AdamW(self.parameters(), lr=opt["lr"], weight_decay=0.06, eps=1e-8)
        self.scheduler = torch.optim.lr_scheduler.OneCycleLR(self.optimizer, max_lr=10 * opt["lr"], total_steps=opt["epochs"] * self.steps, pct_start=0.2, anneal_strategy="cos")

        self.training_step_outputs = []
        self.validation_step_outputs = []

    def get_node_state(self, nodes: Union[List[str], np.ndarray]) -> torch.Tensor:
        node_embedding = self.environment.get_knowledge_embedding(nodes)
        node_state = torch.stack([d["avg_pool"] for d in node_embedding], dim=0)

        return node_state

    # @profile
    def forward(self, batch):
        inp = batch["embedding"]
        input_info = self.inputlayer(inp)
        agent_state = input_info.clone()

        current_node = batch["root"]
        current_activation = torch.zeros(batch["nodes"].shape, dtype=torch.float, device=self.opt["device"])
        current_score = torch.zeros(batch["nodes"].shape, dtype=torch.float, device=self.opt["device"])
        all_node_embedding = torch.nn.utils.rnn.pad_sequence([self.get_node_state(n[n != ""]) for n in batch["nodes"]], batch_first=True)
        all_node_embedding = self.gvt(all_node_embedding, batch["adj"], glob=True)

        result = []
        for step in range(0, self.max_hops):
            agent_state, current_node, current_activation, current_score, prob, node_nll = self.node_selector(
                (batch["nodes"], batch["adj"], batch["label"]), all_node_embedding, input_info, agent_state,
                (current_node, current_activation, current_score), self.training, self.label_smoothing_rate)

            knowledge_pool, knowledge_nll, raw_kp, mean_k = self.knowledge_selector((batch["gold_k"], batch["nodes"]),
                                                                                    agent_state, current_score, self.label_smoothing_rate)

            current_reward, topk_acc, reward_detail = reward_function(self.node_reward, self.knowledge_reward, self.pool_reward, self.base_poolsize,
                                                                      batch["gold_k"], batch["gold_n"], current_node, knowledge_pool, raw_kp)

            result.append((prob, current_reward, node_nll, knowledge_nll))

            if self.early_stop:
                if mean_k < self.avg_pool_size:
                    break

        pool_size = sum([len(kp) for kp in knowledge_pool])
        reward = torch.sum(current_reward)

        if not self.training:
            self.avg_pool_size = pool_size / len(knowledge_pool)

        return result, topk_acc, pool_size, reward, reward_detail, knowledge_pool, raw_kp

    def configure_optimizers(self):
        return [self.optimizer], [self.scheduler]

    def training_step(self, batch, batch_idx):
        result, topk_acc, avg_pool_size, reward, reward_detail, knowledge_pool, raw_kp = self.forward(batch)
        loss, loss_detail = loss_function(result)
        num_steps = len(loss)
        loss = sum(loss) / num_steps
        loss_detail = loss_detail / num_steps
        self.log("Train Loss", loss, on_step=False, on_epoch=True, logger=True, batch_size=self.batch_size)
        self.training_step_outputs.append((topk_acc, avg_pool_size, reward, reward_detail, loss_detail, num_steps))

        return loss

    def on_train_epoch_end(self):
        self.log_outputs(self.training_step_outputs, "Train", num=self.samples[0])
        self.training_step_outputs.clear()  # free memory

    def validation_step(self, batch, batch_idx, dataloader_idx=0):
        result, topk_acc, avg_pool_size, reward, reward_detail, knowledge_pool, raw_kp = self.forward(batch)
        loss, loss_detail = loss_function(result)
        num_steps = len(loss)
        loss = sum(loss) / num_steps
        loss_detail = loss_detail / num_steps
        self.log("Val Loss", loss, on_step=False, on_epoch=True, logger=True, batch_size=self.batch_size)
        self.validation_step_outputs.append({dataloader_idx: (topk_acc, avg_pool_size, reward, reward_detail, loss_detail, num_steps)})

        return loss

    def on_validation_epoch_end(self):
        idx_list = [list(i.keys())[0] for i in self.validation_step_outputs]
        if len(self.samples) == 5:
            idx = [sum([i == 0 for i in idx_list]), sum([i == 1 for i in idx_list]), sum([i == 2 for i in idx_list]), sum([i == 3 for i in idx_list])]
            idx = [sum(idx[0:i + 1]) for i in range(len(idx))]
            self.log_outputs([list(i.values())[0] for i in self.validation_step_outputs[0:idx[0]]], "Val_Seen", num=self.samples[1])
            self.log_outputs([list(i.values())[0] for i in self.validation_step_outputs[idx[0]:idx[1]]], "Val_UnSeen", num=self.samples[2])
            self.log_outputs([list(i.values())[0] for i in self.validation_step_outputs[idx[1]:idx[2]]], "Test_Seen", num=self.samples[3])
            self.log_outputs([list(i.values())[0] for i in self.validation_step_outputs[idx[2]:idx[3]]], "Test_UnSeen", num=self.samples[4])
        else:
            idx = [sum([i == 0 for i in idx_list]), sum([i == 1 for i in idx_list])]
            idx = [sum(idx[0:i + 1]) for i in range(len(idx))]
            self.log_outputs([list(i.values())[0] for i in self.validation_step_outputs[0:idx[0]]], "Val", num=self.samples[1])
            self.log_outputs([list(i.values())[0] for i in self.validation_step_outputs[idx[0]:idx[1]]], "Test", num=self.samples[2])
        self.validation_step_outputs.clear()  # free memory

    def log_outputs(self, step_outputs, split, num):
        loss_detail = np.zeros(3, dtype=float)
        reward_detail = np.zeros(3, dtype=float)
        acc = np.zeros(4, dtype=float)
        avg_pool_size = 0
        avg_steps = 0
        reward = 0
        cnt = 0
        for output in step_outputs:
            topk_acc, pool_size, batch_reward, batch_reward_detail, batch_loss_detail, num_steps = output
            loss_detail += np.sum(batch_loss_detail, axis=0)
            reward_detail += np.mean(batch_reward_detail, axis=1)
            avg_steps += num_steps

            acc += topk_acc
            avg_pool_size += pool_size
            reward += batch_reward
            cnt += 1
        if num > 0:
            loss_detail /= cnt
            reward_detail /= cnt
            avg_steps /= cnt

            acc /= num
            avg_pool_size /= num
            reward /= num

        if self.training:
            self.avg_pool_size = avg_pool_size
        self.log("{} reward".format(split), reward, on_epoch=True, logger=True)
        self.log("{} avg_pool_size".format(split), avg_pool_size, on_epoch=True, logger=True)
        self.log("{} avg_steps".format(split), avg_steps, on_epoch=True, logger=True)
        for idx, k in enumerate(["1", "5", "10", "All"]):
            self.log("{} top-{}".format(split, k), acc[idx], on_epoch=True, prog_bar=True, logger=True)
        for idx, name in enumerate(["Walk Loss", "Node Loss", "Knowledge Loss"]):
            self.log("{} {}".format(split, name), loss_detail[idx], on_epoch=True, prog_bar=True, logger=True)
        for idx, name in enumerate(["Node reward", "Knowledge reward", "Pool reward"]):
            self.log("{} {}".format(split, name), reward_detail[idx], on_epoch=True, prog_bar=True, logger=True)