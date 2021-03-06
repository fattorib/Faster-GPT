from functools import partial
import torch
import torch.nn as nn
import copy
import math
import torch.nn.functional as F
from typing import Union, Tuple, List

"""
Module class for GPT2. Follows paper specifications wherever possible.
Certain sections of code are copied from the following repos:
    https://github.com/karpathy/minGPT
    https://github.com/EleutherAI/gpt-neox
    https://github.com/ofirpress/attention_with_linear_biases
"""

# GPT2-Style weight initialization (scaling residual layers by 1/sqrt(N))
def _weights_init(m, num_layers):
    if isinstance(m, (nn.Linear)):
        m.weight.data.normal_(mean=0.0, std=0.02)
        if isinstance(m, nn.Linear) and m.bias is not None:
            nn.init.constant_(m.bias, 0)

        if isinstance(m, nn.Linear) and m.bias is None:
            m.weight.data.normal_(mean=0.0, std=0.02)

    if isinstance(m, (nn.Embedding)):
        m.weight.data.normal_(mean=0.0, std=0.02)

    elif isinstance(m, nn.LayerNorm):
        nn.init.zeros_(m.bias)
        nn.init.ones_(m.weight)

    for name, p in m.named_parameters():
        if "fc_resid" in name and "weight" in name:
            p.data.normal_(mean=0.0, std=(0.02 / math.sqrt(num_layers)))


def _embedding_init(m):
    if isinstance(m, (nn.Embedding)):
        m.weight.data.normal_(mean=0.0, std=0.02)
    if isinstance(m, nn.Linear) and m.bias is None:
        m.weight.data.normal_(mean=0.0, std=0.02)


class MLPBlock(nn.Module):
    def __init__(self, dim1: int, dim2: int, p: float, num_layers: int) -> None:
        """An MLP block.

        Args:
            dim1 (int): Input dimension
            dim2 (int): Output dimension
            p (float): Dropout probability
            num_layers (int): Number of total module layers. Used for weight initialization

        """
        super().__init__()
        self.dim1 = dim1
        self.dim2 = dim2
        self.p = p
        self.num_layers = num_layers

        self.gelu = nn.GELU()
        self.fc1 = nn.Linear(self.dim1, self.dim2)
        self.fc_resid = nn.Linear(self.dim2, self.dim1)
        self.dropout = nn.Dropout(p=self.p)

        init_function_partial = partial(
            _weights_init, **{"num_layers": self.num_layers}
        )

        self.apply(init_function_partial)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.fc1(x)
        x = self.gelu(x)
        x = self.fc_resid(x)
        return self.dropout(x)


class ALiBi(nn.Module):
    """
    Self-attention module with ALiBi as described in paper
    `From Train Short, Test Long: Attention with Linear Biases Enables Input
    Length Extrapolation <https://ofir.io/train_short_test_long.pdf>`

    Source code modified from
    <https://github.com/ofirpress/attention_with_linear_biases> and
    <https://github.com/EleutherAI/gpt-neox/blob/main/megatron/model/positional_embeddings.py>

    """

    def __init__(
        self,
        embedding_dim: int,
        num_head: int,
        block_size: int,
        resid_dropout: float,
        num_layers: int,
    ):
        super().__init__()
        assert embedding_dim % num_head == 0
        # key, query, value projections for all heads
        self.key = nn.Linear(embedding_dim, embedding_dim)
        self.query = nn.Linear(embedding_dim, embedding_dim)
        self.value = nn.Linear(embedding_dim, embedding_dim)
        # regularization
        self.resid_drop = nn.Dropout(resid_dropout)
        # output projection
        self.fc_resid = nn.Linear(embedding_dim, embedding_dim)

        self.alibi_cache = None
        self.cached_ctx = None

        self.n_head = num_head
        self.num_layers = num_layers

        self.register_buffer(
            "slopes", torch.Tensor(self.get_slopes(self.n_head))
        )
        self.register_buffer(
            "mask",
            torch.tril(
                torch.ones(block_size, block_size, dtype=torch.uint8)
            ).view(1, 1, block_size, block_size),
        )

        init_function_partial = partial(
            _weights_init, **{"num_layers": self.num_layers}
        )

        self.apply(init_function_partial)

    def get_slopes(self, n: int) -> List:
        def get_slopes_power_of_2(n):
            start = 2 ** (-(2 ** -(math.log2(n) - 3)))
            ratio = start
            return [start * ratio ** i for i in range(n)]

        if math.log2(n).is_integer():
            return get_slopes_power_of_2(n)
        else:
            closest_power_of_2 = 2 ** math.floor(math.log2(n))
            return (
                get_slopes_power_of_2(closest_power_of_2)
                + self.get_slopes(2 * closest_power_of_2)[0::2][
                    : n - closest_power_of_2
                ]
            )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B, T, C = x.size()
        k = (
            self.key(x)
            .view(B, T, self.n_head, C // self.n_head)
            .transpose(1, 2)
        )  # (B, nh, T, hs)
        q = (
            self.query(x)
            .view(B, T, self.n_head, C // self.n_head)
            .transpose(1, 2)
        )  # (B, nh, T, hs)
        v = (
            self.value(x)
            .view(B, T, self.n_head, C // self.n_head)
            .transpose(1, 2)
        )  # (B, nh, T, hs)

        # causal self-attention; Self-attend: (B, nh, T, hs) x (B, nh, hs, T) -> (B, nh, T, T)
        att = (q @ k.transpose(-2, -1)) * (1.0 / math.sqrt(k.size(-1)))

        # Creation of ALiBi distance matrix -> Computed on first forward pass
        # and stored. If CTX changes, we update this
        if self.cached_ctx != T:
            # Update Buffer mask
            self.mask = (
                torch.tril(torch.ones(T, T, dtype=torch.uint8))
                .view(1, 1, T, T)
                .to(x.device)
            )

            # Create ALiBi distance matrix
            a = -torch.tril(
                torch.arange(T).view(T, 1).repeat(1, T)
                + torch.arange(0, -T, -1)
            )
            a = a.to(x.device).to(x.dtype)

            self.alibi_cache = a * self.slopes.view(self.slopes.shape[0], 1, 1)
            self.cached_ctx = T

        att = att + self.alibi_cache

        att = att.masked_fill(self.mask[:, :, :T, :T] == 0, float("-inf"))
        att = F.softmax(att, dim=-1)
        y = att @ v  # (B, nh, T, T) x (B, nh, T, hs) -> (B, nh, T, hs)
        y = (
            y.transpose(1, 2).contiguous().view(B, T, C)
        )  # re-assemble all head outputs side by side

        # output projection
        y = self.resid_drop(self.fc_resid(y))
        return y


class CausalSelfAttention(nn.Module):
    """
    A vanilla multi-head masked self-attention layer with a projection at the end.

    Minor modifications from `https://github.com/karpathy/minGPT/`
    """

    def __init__(
        self,
        embedding_dim: int,
        num_head: int,
        block_size: int,
        resid_dropout: float,
        num_layers: int,
    ) -> None:
        super().__init__()
        assert embedding_dim % num_head == 0
        # key, query, value projections for all heads
        self.key = nn.Linear(embedding_dim, embedding_dim)
        self.query = nn.Linear(embedding_dim, embedding_dim)
        self.value = nn.Linear(embedding_dim, embedding_dim)
        self.attn_drop = nn.Dropout(resid_dropout)
        self.resid_drop = nn.Dropout(resid_dropout)
        self.fc_resid = nn.Linear(embedding_dim, embedding_dim)
        self.register_buffer(
            "mask",
            torch.tril(
                torch.ones(block_size, block_size, dtype=torch.uint8)
            ).view(1, 1, block_size, block_size),
        )
        self.n_head = num_head
        self.num_layers = num_layers

        init_function_partial = partial(
            _weights_init, **{"num_layers": self.num_layers}
        )

        self.apply(init_function_partial)

    def forward(
        self,
        x: torch.Tensor,
    ) -> torch.Tensor:
        B, T, C = x.size()

        k = (
            self.key(x)
            .view(B, T, self.n_head, C // self.n_head)
            .transpose(1, 2)
        )  # (B, nh, T, hs)
        q = (
            self.query(x)
            .view(B, T, self.n_head, C // self.n_head)
            .transpose(1, 2)
        )  # (B, nh, T, hs)
        v = (
            self.value(x)
            .view(B, T, self.n_head, C // self.n_head)
            .transpose(1, 2)
        )  # (B, nh, T, hs)

        # causal self-attention; Self-attend: (B, nh, T, hs) x (B, nh, hs, T) -> (B, nh, T, T)
        att = (q @ k.transpose(-2, -1)) * (1.0 / math.sqrt(k.size(-1)))
        att = att.masked_fill(self.mask[:, :, :T, :T] == 0, float("-inf"))
        att = F.softmax(att, dim=-1)
        att = self.attn_drop(att)
        y = att @ v  # (B, nh, T, T) x (B, nh, T, hs) -> (B, nh, T, hs)
        y = (
            y.transpose(1, 2).contiguous().view(B, T, C)
        )  # re-assemble all head outputs side by side

        # output projection
        y = self.resid_drop(self.fc_resid(y))
        return y


class GPT2Block(nn.Module):
    """
    Standard Transformer block

    Based on `https://github.com/karpathy/minGPT/` with modifications
    """

    def __init__(
        self,
        embedding_dim: int,
        num_head: int,
        block_size: int,
        resid_dropout: float,
        num_layers: int,
        fused_residuals: bool,
        use_alibi: bool,
    ) -> None:
        super().__init__()
        self.ln1 = nn.LayerNorm(embedding_dim)
        self.fused_residuals = fused_residuals

        if not self.fused_residuals:
            self.ln2 = nn.LayerNorm(embedding_dim)

        if use_alibi:
            self.attn = ALiBi(
                embedding_dim, num_head, block_size, resid_dropout, num_layers
            )

        else:
            self.attn = CausalSelfAttention(
                embedding_dim, num_head, block_size, resid_dropout, num_layers
            )

        self.mlp = MLPBlock(
            embedding_dim,
            4 * embedding_dim,
            resid_dropout,
            num_layers,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.fused_residuals:
            x = x + self.mlp(self.ln1(x)) + self.attn(self.ln1(x))
        else:
            x = x + self.attn(self.ln1(x))
            x = x + self.mlp(self.ln2(x))
        return x


class GPT2(nn.Module):
    def __init__(
        self,
        num_ctx: int,
        embedding_dim: int,
        N: int,
        vocab_size: int,
        num_head: int = 12,
        fused_residuals: bool = False,
        mlp_dropout: float = 0.0,
        resid_dropout: float = 0.0,
        embedding_dropout: float = 0.0,
        use_alibi: bool = False,
    ):
        super().__init__()
        self.num_ctx = num_ctx
        self.embedding_dim = embedding_dim
        self.N = N
        self.vocab_size = vocab_size
        self.mlp_dropout = mlp_dropout
        self.resid_dropout = resid_dropout
        self.embedding_dropout = embedding_dropout
        self.num_head = num_head
        self.use_alibi = use_alibi
        self.fused_residuals = fused_residuals

        """
        Basic GPT2 transformer module
        """

        self.wte = nn.Embedding(self.vocab_size, self.embedding_dim)

        if not self.use_alibi:
            self.wpe = nn.Embedding(self.num_ctx, self.embedding_dim)

        self.dropout = nn.Dropout(p=self.embedding_dropout)

        self.blocks = nn.ModuleList(
            [
                copy.deepcopy(
                    GPT2Block(
                        embedding_dim=embedding_dim,
                        num_head=self.num_head,
                        block_size=self.num_ctx,
                        resid_dropout=resid_dropout,
                        num_layers=N,
                        fused_residuals=fused_residuals,
                        use_alibi=self.use_alibi,
                    )
                )
                for _ in range(self.N)
            ]
        )

        self.norm = nn.LayerNorm(self.embedding_dim)

        embed_shape = self.wte.weight.shape
        self.lm_head = nn.Linear(
            in_features=embed_shape[1], out_features=embed_shape[0], bias=False
        )

        # Tying embedding weights
        self.lm_head.weight = self.wte.weight

        self.apply(_embedding_init)

    def generate(
        self, context: torch.Tensor, max_length: int, sample: bool = False
    ) -> torch.Tensor:
        """
        Small generation method for compatibility with LM-Eval harness. Defaults
        to greedy decoding

        Parameters:
            context ('torch.Tensor'):
                Input context to prime the model

            max_length ('int'):
                The maximum length of tokens to generate (sum of context + *generated tokens*)

            sample ('bool'):
                Bool whether to sample from logits distribution
        """

        context = torch.tensor(context, dtype=torch.long).to(
            self.wte.weight.device
        )

        x = context.view(1, -1)

        num_generation_steps = max_length - x.shape[1]

        for _ in range(num_generation_steps):

            if x.shape[1] > self.num_ctx:
                x_cond = x[:, -self.num_ctx :]
            else:
                x_cond = x

            with torch.no_grad():
                with torch.cuda.amp.autocast():
                    logits = self.forward(x_cond)

                logits = logits[:, -1, :]

                probs = F.softmax(logits, dim=-1)

                if not sample:
                    out = torch.topk(probs, k=1)
                    x = torch.cat((x[:, :], out.indices), axis=1)
                else:
                    out = torch.multinomial(probs, num_samples=1)
                    x = torch.cat((x[:, :], out), axis=1)

        return x

    def forward(
        self,
        x: torch.Tensor,
        labels: torch.Tensor = None,
    ) -> Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]:

        b, t = x.size()

        if not self.use_alibi:
            position_ids = torch.arange(0, t, dtype=torch.long, device=x.device)
            position_ids = position_ids.unsqueeze(0).view(-1, t)

            position_embeds = self.wpe(position_ids)

        x = self.wte(x)

        if not self.use_alibi:
            x = self.dropout(x + position_embeds)
        else:
            x = self.dropout(x)

        for block in self.blocks:
            x = block(x)

        x = self.norm(x)

        logits_lm = self.lm_head(x)

        loss = None
        if labels is not None:
            # Shift so that tokens < n predict n
            shift_logits = logits_lm[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            # Flatten the tokens
            loss_fct = torch.nn.CrossEntropyLoss()
            loss = loss_fct(
                shift_logits.view(-1, shift_logits.size(-1)),
                shift_labels.view(-1),
            )

            return logits_lm, loss
        else:
            return logits_lm


def create_GPT2_qa(vocab_size, num_ctx, **kwargs):
    """
    QA model for testing
    """
    return GPT2(
        num_ctx=num_ctx, embedding_dim=128, N=4, vocab_size=vocab_size, **kwargs
    )


def create_GPT2_base(vocab_size, num_ctx, **kwargs):
    """
    Matches the parameters of the original GPT2-117M model
    """
    return GPT2(
        num_ctx=num_ctx,
        embedding_dim=768,
        N=12,
        vocab_size=vocab_size,
        **kwargs
    )


def create_GPT2_medium(vocab_size, num_ctx, **kwargs):
    """
    Matches the parameters of the original GPT2-345M model
    """
    return GPT2(
        num_ctx=num_ctx,
        embedding_dim=1024,
        N=24,
        vocab_size=vocab_size,
        num_head=16,
        **kwargs
    )


def create_GPT2_base_optimized(vocab_size, num_ctx, **kwargs):
    """
    Updated GPT-medium model optimized for increased throughput.
    The following changes have been made:
        1. Parallel Residual layers
        2. Increased embedding dimension and head dimension (decreased num_heads)
        3. Decreased model depth to hold params ~constant
        4. Decreased train ctx due to ALiBi

    """
    return GPT2(
        num_ctx=num_ctx,
        embedding_dim=1024,
        N=6,
        vocab_size=vocab_size,
        **kwargs
    )


def create_GPT2_medium_optimized(vocab_size, num_ctx, **kwargs):
    """
    Updated GPT-medium model optimized for increased throughput.
    The following changes have been made:
        1. Parallel Residual layers
        2. Increased embedding dimension and head dimension (decreased num_heads)
        3. Decreased model depth to hold params ~constant
        4. Decreased train ctx due to ALiBi

    """
    return GPT2(
        num_ctx=num_ctx,
        embedding_dim=1536,
        N=8,
        vocab_size=vocab_size,
        **kwargs
    )


def model_getter(model_name, vocab_size, num_ctx, **kwargs):
    MODELS_DICT = {
        "qa": create_GPT2_qa,
        "base": create_GPT2_base,
        "medium": create_GPT2_medium,
        "base*": create_GPT2_base_optimized,
        "medium*": create_GPT2_medium_optimized,
    }

    return MODELS_DICT[model_name](vocab_size, num_ctx, **kwargs)
