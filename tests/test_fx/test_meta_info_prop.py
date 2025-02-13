import torch
import torch.nn as nn
import colossalai
import colossalai.nn as col_nn
from torch.fx import symbolic_trace
from colossalai.fx.passes.meta_info_prop import MetaInfoProp, TensorMetadata

BATCH_SIZE = 2
DIM_IN = 4
DIM_OUT = 16


def meta_check(meta_info_spec: TensorMetadata, orig_tensor: torch.Tensor):
    assert meta_info_spec.shape == orig_tensor.shape
    assert meta_info_spec.dtype == orig_tensor.dtype
    assert meta_info_spec.requires_grad == orig_tensor.requires_grad
    assert meta_info_spec.stride == orig_tensor.stride()
    assert meta_info_spec.numel == orig_tensor.numel()


def test_meta_info_prop():
    model = torch.nn.Linear(DIM_IN, DIM_OUT)
    input_sample = torch.rand(BATCH_SIZE, DIM_IN)
    orig_output = model(input_sample)
    gm = symbolic_trace(model)
    for node in gm.graph.nodes:
        assert not hasattr(node,
                           'node_size'), 'The attribute Node.node_size should not exist before MetaInfoProp procedure'
        assert not hasattr(node,
                           'param_size'), 'The attribute Node.param_size should not exist before MetaInfoProp procedure'
        assert not hasattr(
            node,
            'activation_size'), 'The attribute Node.activation_size should not exist before MetaInfoProp procedure'
    MetaInfoProp(gm).run(input_sample)
    for node in gm.graph.nodes:
        if node.op == 'placeholder':
            meta_check(node.meta['tensor_meta'], input_sample)
        if node.op == 'output':
            meta_check(node.meta['tensor_meta'], orig_output)
        assert hasattr(node, 'node_size'), 'The attribute Node.node_size should exist after MetaInfoProp procedure'
        assert hasattr(node, 'param_size'), 'The attribute Node.param_size should exist after MetaInfoProp procedure'
        assert hasattr(
            node, 'activation_size'), 'The attribute Node.activation_size should exist after MetaInfoProp procedure'


if __name__ == '__main__':
    test_meta_info_prop()
