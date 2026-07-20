"""Small GRU for lap-time regression + stub trajectory head."""

from __future__ import annotations

import torch
import torch.nn as nn


class SharedGRUEncoder(nn.Module):
    def __init__(
        self,
        input_size: int,
        hidden_size: int = 64,
        num_layers: int = 1,
        dropout: float = 0.2,
    ) -> None:
        super().__init__()
        self.gru = nn.GRU(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,
            dropout=dropout if num_layers > 1 else 0.0,
        )
        self.hidden_size = hidden_size

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (B, T, F) -> last hidden (B, H)
        _, h_n = self.gru(x)
        return h_n[-1]


class LapTimeGRU(nn.Module):
    def __init__(
        self,
        input_size: int,
        hidden_size: int = 64,
        num_layers: int = 1,
        dropout: float = 0.2,
    ) -> None:
        super().__init__()
        self.encoder = SharedGRUEncoder(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            dropout=dropout,
        )
        self.head = nn.Sequential(
            nn.Dropout(dropout),
            nn.Linear(hidden_size, 1),
        )
        # Secondary task stub — not trained in phase 1
        self.trajectory_head = nn.Sequential(
            nn.Dropout(dropout),
            nn.Linear(hidden_size, 2),  # future (dx, dy) placeholder
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h = self.encoder(x)
        return self.head(h).squeeze(-1)

    def forward_trajectory_stub(self, x: torch.Tensor) -> torch.Tensor:
        """Phase-2 stub: predict relative (dx, dy). Not used in training yet."""
        h = self.encoder(x)
        return self.trajectory_head(h)
