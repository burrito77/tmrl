# from collections import deque
from copy import deepcopy, copy
from dataclasses import dataclass, InitVar
from functools import lru_cache, reduce
# from itertools import chain
import numpy as np
import torch
from torch.nn.functional import mse_loss

from agents.memory_dataloading import Memory
# from agents.memory import Memory
from agents.nn import PopArt, no_grad, copy_shared, exponential_moving_average, hd_conv
from agents.util import cached_property, partial
import agents.sac_models


@dataclass(eq=0)
class Agent:
    Env: InitVar

    Model: type = agents.sac_models.Mlp
    OutputNorm: type = PopArt
    batchsize: int = 256  # training batch size
    memory_size: int = 1000000  # replay memory size
    lr: float = 0.0003  # learning rate
    discount: float = 0.99  # reward discount factor
    target_update: float = 0.005  # parameter for exponential moving average
    reward_scale: float = 5.
    entropy_scale: float = 1.
    device: str = None
    observation_space = None
    action_space = None
    path_loc: str = r"D:\data"
    imgs_obs: int = 4

    model_nograd = cached_property(lambda self: no_grad(copy_shared(self.model)))

    # total_updates = 0
    # environment_steps = 0

    def __post_init__(self, Env):
        if Env is not None:
            with Env() as env:
                observation_space, action_space = env.observation_space, env.action_space
        else:
            observation_space, action_space = self.observation_space, self.action_space
        device = self.device or ("cuda" if torch.cuda.is_available() else "cpu")
        model = self.Model(observation_space, action_space)
        self.model = model.to(device)
        self.model_target = no_grad(deepcopy(self.model))

        self.actor_optimizer = torch.optim.Adam(self.model.actor.parameters(), lr=self.lr)
        self.critic_optimizer = torch.optim.Adam(self.model.critics.parameters(), lr=self.lr)
        self.memory = Memory(self.memory_size, self.batchsize, device, path_loc=self.path_loc, imgs_obs=self.imgs_obs)

        self.outputnorm = self.OutputNorm(self.model.critic_output_layers)
        self.outputnorm_target = self.OutputNorm(self.model_target.critic_output_layers)

    def act(self, state, obs, r, done, info, train=False):
        state = self.model.reset() if state is None else state  # initialize state if necessary
        action, next_state, _ = self.model.act(state, obs, r, done, info, train)
        if train:
            self.memory.append(np.float32(r), np.float32(done), info, obs, action)
        return action, next_state

    def train(self):
        obs, actions, rewards, next_obs, terminals = self.memory.sample()  # sample a transition from the replay buffer
        new_action_distribution = self.model.actor(obs)  # outputs distribution object
        new_actions = new_action_distribution.rsample()  # samples using the reparametrization trick

        # critic loss
        next_action_distribution = self.model_nograd.actor(next_obs)  # outputs distribution object
        next_actions = next_action_distribution.sample()  # samples
        next_value = [c(next_obs, next_actions) for c in self.model_target.critics]
        next_value = reduce(torch.min, next_value)  # minimum action-value
        next_value = self.outputnorm_target.unnormalize(next_value)  # PopArt (not present in the original paper)
        # next_value = self.outputnorm.unnormalize(next_value)  # PopArt (not present in the original paper)

        # predict entropy rewards in a separate dimension from the normal rewards (not present in the original paper)
        next_action_entropy = - (1. - terminals) * self.discount * next_action_distribution.log_prob(next_actions)
        reward_components = torch.cat((
            self.reward_scale * rewards[:, None],
            self.entropy_scale * next_action_entropy[:, None],
        ), dim=1)  # shape = (batchsize, reward_components)

        value_target = reward_components + (1. - terminals[:, None]) * self.discount * next_value
        normalized_value_target = self.outputnorm.update(value_target)  # PopArt update and normalize

        values = [c(obs, actions) for c in self.model.critics]
        assert values[0].shape == normalized_value_target.shape and not normalized_value_target.requires_grad
        loss_critic = sum(mse_loss(v, normalized_value_target) for v in values)

        # update critic
        self.critic_optimizer.zero_grad()
        loss_critic.backward()
        self.critic_optimizer.step()

        # actor loss
        new_value = [c(obs, new_actions) for c in self.model.critics]  # new_actions with reparametrization trick
        new_value = reduce(torch.min, new_value)  # minimum action_values
        assert new_value.shape == (self.batchsize, 2)

        new_value = self.outputnorm.unnormalize(new_value)
        new_value[:, -1] -= self.entropy_scale * new_action_distribution.log_prob(new_actions)
        loss_actor = - self.outputnorm.normalize_sum(new_value.sum(1)).mean()  # normalize_sum preserves relative scale

        # update actor
        self.actor_optimizer.zero_grad()
        loss_actor.backward()
        self.actor_optimizer.step()

        # update target critics and normalizers
        exponential_moving_average(self.model_target.critics.parameters(), self.model.critics.parameters(), self.target_update)
        exponential_moving_average(self.outputnorm_target.parameters(), self.outputnorm.parameters(), self.target_update)

        return dict(
            loss_actor=loss_actor.detach(),
            loss_critic=loss_critic.detach(),
            outputnorm_reward_mean=self.outputnorm.mean[0],
            outputnorm_entropy_mean=self.outputnorm.mean[-1],
            outputnorm_reward_std=self.outputnorm.std[0],
            outputnorm_entropy_std=self.outputnorm.std[-1],
            memory_size=len(self.memory),
        )


AvenueAgent = partial(
    Agent,
    entropy_scale=0.05,
    lr=0.0002,
    memory_size=500000,
    batchsize=100,
    # training_steps=1 / 4,
    # start_training=10000,
    Model=partial(agents.sac_models.ConvModel)
)


# === tests ============================================================================================================
# def test_agent():
#     from agents import Training, run
#     Sac_Test = partial(
#         Training,
#         epochs=3,
#         rounds=5,
#         steps=100,
#         start_training=256,
#         Agent=partial(Agent, device='cpu', memory_size=1000000, batchsize=4),
#         Env=partial(id="Pendulum-v0", real_time=0),
#     )
#     run(Sac_Test)


# def test_agent_avenue():
#     from agents import Training, run
#     from agents.envs import AvenueEnv
#     Sac_Avenue_Test = partial(
#         Training,
#         epochs=3,
#         rounds=5,
#         steps=300,
#         Agent=partial(AvenueAgent, device='cpu', training_interval=4, start_training=400),
#         Env=partial(AvenueEnv, real_time=0),
#         Test=partial(number=0),  # laptop can't handle more than that
#     )
#     run(Sac_Avenue_Test)
#
#
# def test_agent_avenue_hd():
#     from agents import Training, run
#     from agents.envs import AvenueEnv
#     Sac_Avenue_Test = partial(
#         Training,
#         epochs=3,
#         rounds=5,
#         steps=300,
#         Agent=partial(AvenueAgent, device='cpu', training_interval=4, start_training=400, Model=partial(Conv=hd_conv)),
#         Env=partial(AvenueEnv, real_time=0, width=368, height=368),
#         Test=partial(number=0),  # laptop can't handle more than that
#     )
#     run(Sac_Avenue_Test)


# if __name__ == "__main__":
#     test_agent()
# test_agent_avenue()
# test_agent_avenue_hd()
