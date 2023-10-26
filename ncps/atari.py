import gymnasium as gym
import torch
from stable_baselines3.common.atari_wrappers import (
    ClipRewardEnv,
    EpisodicLifeEnv,
    FireResetEnv,
    MaxAndSkipEnv,
    NoopResetEnv,
)
from torch.utils.data import Dataset
import torch.optim as optim

import numpy as np
import torch.nn as nn
import torch.nn.functional as F
from tqdm import tqdm

from ncps.torch import CfC
from ncps.datasets.torch import AtariCloningDataset


class ConvBlock(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv1 = nn.Conv2d(4, 64, 5, padding=2, stride=2)
        self.conv2 = nn.Conv2d(64, 128, 5, padding=2, stride=2)
        self.bn2 = nn.BatchNorm2d(128)
        self.conv3 = nn.Conv2d(128, 128, 5, padding=2, stride=2)
        self.conv4 = nn.Conv2d(128, 256, 5, padding=2, stride=2)
        self.bn4 = nn.BatchNorm2d(256)

    def forward(self, x):
        x = F.relu(self.conv1(x))
        x = F.relu(self.bn2(self.conv2(x)))
        x = F.relu(self.conv3(x))
        x = F.relu(self.bn4(self.conv4(x)))
        x = x.mean((-1, -2))  # Global average pooling
        return x


class ConvCfC(nn.Module):
    def __init__(self, n_actions):
        super().__init__()
        self.conv_block = ConvBlock()
        self.rnn = CfC(256, 64, batch_first=True, proj_size=n_actions)

    def forward(self, x, hx=None):
        batch_size = x.size(0)
        seq_len = x.size(1)
        # Merge time and batch dimension into a single one (because the Conv layers require this)
        x = x.view(batch_size * seq_len, *x.shape[2:])
        x = self.conv_block(x)  # apply conv block to merged data
        # Separate time and batch dimension again
        x = x.view(batch_size, seq_len, *x.shape[1:])
        x, hx = self.rnn(x, hx)  # hx is the hidden state of the RNN
        return x, hx


def eval(model, valloader):
    losses, accs = [], []
    model.eval()
    device = next(model.parameters()).device  # get device the model is located on
    with torch.no_grad():
        for i, (inputs, labels) in enumerate(valloader):
            inputs = inputs.to(device)  # move data to same device as the model
            labels = labels.to(device)

            outputs, _ = model(inputs)
            outputs = outputs.reshape(-1, *outputs.shape[2:])  # flatten
            labels = labels.view(-1, *labels.shape[2:])  # flatten
            loss = criterion(outputs, labels)
            acc = (outputs.argmax(-1) == labels).float().mean()
            losses.append(loss.item())
            accs.append(acc.item())

            if i >= 3:
                break

    return np.mean(losses), np.mean(accs)


def train_one_epoch(model, criterion, optimizer, trainloader):
    running_loss = 0.0
    pbar = tqdm(total=len(trainloader))
    model.train()
    device = next(model.parameters()).device  # get device the model is located on
    for i, (inputs, labels) in enumerate(trainloader):
        inputs = inputs.to(device)  # move data to same device as the model
        labels = labels.to(device)

        # zero the parameter gradients
        optimizer.zero_grad()
        # forward + backward + optimize
        outputs, hx = model(inputs)
        labels = labels.view(-1, *labels.shape[2:])  # flatten
        outputs = outputs.reshape(-1, *outputs.shape[2:])  # flatten
        loss = criterion(outputs, labels)
        loss.backward()
        optimizer.step()

        # print statistics
        running_loss += loss.item()
        pbar.set_description(f"loss={running_loss / (i + 1):0.4g}")
        pbar.update(1)

        if i >= 3:
            break
    pbar.close()


def run_closed_loop(model, env, num_episodes=None):
    obs, info = env.reset()
    device = next(model.parameters()).device
    hx = None  # Hidden state of the RNN
    returns = []
    total_reward = 0
    with torch.no_grad():
        while True:
            # PyTorch require channel first images -> transpose data
            obs = np.transpose(obs, [0, 1, 2]).astype(np.float32)
            # Observation seems to be already normalized, see: https://github.com/mlech26l/ncps/issues/48#issuecomment-1572328370
            # obs = np.transpose(obs, [2, 0, 1]).astype(np.float32) / 255.0
            # add batch and time dimension (with a single element in each)
            obs = torch.from_numpy(obs).unsqueeze(0).unsqueeze(0).to(device)
            pred, hx = model(obs, hx)
            # remove time and batch dimension -> then argmax
            action = pred.squeeze(0).squeeze(0).argmax().item()
            obs, r, terminated, truncated, info = env.step(action)
            done = terminated or truncated
            total_reward += r
            if done:
                obs, info = env.reset()
                hx = None  # Reset hidden state of the RNN
                returns.append(total_reward)
                total_reward = 0
                if num_episodes is not None:
                    # Count down the number of episodes
                    num_episodes = num_episodes - 1
                    if num_episodes == 0:
                        return returns


def wrap_deepmind2(env_id, dim=84, capture_video=True, render_mode=None, framestack=True, noframeskip=False):
    """Configure environment for DeepMind-style Atari.

    Note that we assume reward clipping is done outside the wrapper.

    Args:
        env: The env object to wrap.
        dim: Dimension to resize observations to (dim x dim).
        framestack: Whether to framestack observations.
    """
    if capture_video:
        env = gym.make(env_id, render_mode="rgb_array")
        env = gym.wrappers.RecordVideo(env, f"videos/{env_id}")
    else:
        env = gym.make(env_id, render_mode=render_mode)
    env = gym.wrappers.RecordEpisodeStatistics(env)
    env = NoopResetEnv(env, noop_max=30)
    if env.spec is not None and noframeskip is True:
        env = MaxAndSkipEnv(env, skip=4)
    env = EpisodicLifeEnv(env)
    if "FIRE" in env.unwrapped.get_action_meanings():
        env = FireResetEnv(env)
    env = gym.wrappers.ResizeObservation(env, (dim, dim))
    env = gym.wrappers.GrayScaleObservation(env)
    if framestack is True:
        env = gym.wrappers.FrameStack(env, 4)
    return env

if __name__ == "__main__":
    env = wrap_deepmind2("ALE/Breakout-v5")

    train_ds = AtariCloningDataset("breakout", split="train")
    val_ds = AtariCloningDataset("breakout", split="val")
    trainloader = torch.utils.data.DataLoader(
        train_ds, batch_size=32, num_workers=4, shuffle=True
    )
    valloader = torch.utils.data.DataLoader(val_ds, batch_size=32, num_workers=4)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = ConvCfC(n_actions=env.action_space.n).to(device)
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.Adam(model.parameters(), lr=0.0001)

    for epoch in range(1):  # loop over the dataset multiple times
        train_one_epoch(model, criterion, optimizer, trainloader)
        print('finished training')

        # Evaluate model on the validation set
        val_loss, val_acc = eval(model, valloader)
        print(f"Epoch {epoch+1}, val_loss={val_loss:0.4g}, val_acc={100*val_acc:0.2f}%")

        # Apply model in closed-loop environment
        returns = run_closed_loop(model, env, num_episodes=10)
        print(f"Mean return {np.mean(returns)} (n={len(returns)})")

    # Visualize Atari game and play endlessly
    env = wrap_deepmind2("ALE/Breakout-v5", capture_video=False, render_mode="human")
    run_closed_loop(model, env)