import torch
import torch.nn.functional as F
from typing import List, Optional, Iterator, Tuple

from .freq_aware_embedding import FreqAwareEmbeddingBag
from .cache_mgr import CachedParamMgr
from torch.nn.parameter import Parameter
from colossalai.nn._ops._utils import dual_all_to_all

from colossalai.tensor import ColoParameter, ShardSpec, ComputePattern, ProcessGroup, ColoTensorSpec, ColoTensor


def get_partition(embedding_dim, rank, world_size) -> Tuple[int, int, bool]:
    if world_size == 1:
        return 0, embedding_dim, True

    assert embedding_dim >= world_size, \
        f"Embedding dimension {embedding_dim} must be larger than the world size " \
        f"{world_size} of the process group"
    chunk_size = embedding_dim // world_size
    threshold = embedding_dim % world_size
    # if embedding dim is divisible by world size
    if threshold == 0:
        return rank * chunk_size, (rank + 1) * chunk_size, True

    # align with the split strategy of torch.tensor_split
    size_list = [chunk_size + 1 if i < threshold else chunk_size for i in range(world_size)]
    offset = sum(size_list[:rank])
    return offset, offset + size_list[rank], False


class ParallelFreqAwareEmbeddingBag(FreqAwareEmbeddingBag):

    def __init__(
        self,
        num_embeddings,
        embedding_dim,
        padding_idx=None,
        max_norm=None,
        norm_type=2.,
        scale_grad_by_freq=False,
        sparse=False,
        _weight=None,
        mode='mean',
        include_last_offset=False,
        dtype=None,
        device=None,
        cuda_row_num=0,
        ids_freq_mapping=None,
        warmup_ratio=0.7,
        buffer_size=50_000,
    ):
        self.rank = torch.distributed.get_rank()
        self.world_size = torch.distributed.get_world_size()

        self.partition_start_index, self.partition_end_index, divisible = get_partition(
            embedding_dim, self.rank, self.world_size)
        self.embedding_dim_per_partition = self.partition_end_index - self.partition_start_index

        super(ParallelFreqAwareEmbeddingBag,
              self).__init__(num_embeddings, embedding_dim, padding_idx, max_norm, norm_type, scale_grad_by_freq,
                             sparse, _weight, mode, include_last_offset, dtype, device, cuda_row_num, ids_freq_mapping,
                             warmup_ratio, buffer_size)

    def _weight_alloc(self, dtype, device):
        colo_tensor_spec = ColoTensorSpec(pg=ProcessGroup(tp_degree=self.world_size),
                                          dist_attr=ShardSpec(dims=[-1], num_partitions=[self.world_size]),
                                          compute_attr=ComputePattern.TP1D)
        return ColoTensor.from_torch_tensor(torch.empty(self.num_embeddings,
                                                        self.embedding_dim_per_partition,
                                                        device=device,
                                                        dtype=dtype),
                                            spec=colo_tensor_spec)

    def forward(self, indices, offsets=None, per_sample_weights=None, shape_hook=None, scatter_dim=0, gather_dim=-1):
        with torch.no_grad():
            reorder_ids = self.cache_weight_mgr.prepare_ids(indices)

        output_shard = F.embedding_bag(reorder_ids, self.cache_weight_mgr.cuda_cached_weight, offsets, self.max_norm,
                                       self.norm_type, self.scale_grad_by_freq, self.mode, self.sparse,
                                       per_sample_weights, self.include_last_offset, self.padding_idx)

        if shape_hook is not None:
            output_shard = shape_hook(output_shard)

        output_full = dual_all_to_all(output_shard,
                                      self.weight.get_process_group(),
                                      scatter_dim=scatter_dim,
                                      gather_dim=gather_dim)
        return output_full

    @classmethod
    def from_pretrained(
        cls,
        embedding: torch.Tensor,
        freeze: bool = True,
        padding_idx: Optional[int] = None,
        max_norm: Optional[float] = None,
        norm_type: float = 2.,
        scale_grad_by_freq: bool = False,
        sparse: bool = False,
        mode: str = 'mean',
        include_last_offset: bool = False,
        cuda_row_num: int = 100_000,
        ids_freq_mapping: Optional[List[int]] = None,
        warmup_ratio: float = 0.7,
        buffer_size: int = 50_000,
    ) -> 'ParallelFreqAwareEmbeddingBag':
        rows, cols = embedding.shape
        embedding_bag = cls(rows,
                            cols,
                            padding_idx,
                            max_norm,
                            norm_type,
                            scale_grad_by_freq,
                            sparse,
                            embedding,
                            mode,
                            include_last_offset,
                            cuda_row_num=cuda_row_num,
                            ids_freq_mapping=ids_freq_mapping,
                            warmup_ratio=warmup_ratio,
                            buffer_size=buffer_size)
        embedding_bag.cache_weight_mgr.cuda_cached_weight.requires_grad_ = not freeze
        return embedding_bag
