import pytest
import torch

from olmo_core.distributed.fsdp.stream import CudaStream, Stream


@pytest.mark.skipif(not torch.cuda.is_available(), reason="Requires a GPU")
@pytest.mark.gpu
def test_cuda_stream():
    device = torch.device("cuda")

    default_stream = Stream.default(device)
    assert isinstance(default_stream, CudaStream)
    assert isinstance(default_stream.base_stream, torch.cuda.Stream)

    current_stream = Stream.current(device)
    assert isinstance(current_stream, CudaStream)
    assert isinstance(current_stream.base_stream, torch.cuda.Stream)

    other_stream = Stream.new(device)
    assert isinstance(other_stream, CudaStream)

    x = torch.empty((100, 100), device=device).normal_(0.0, 1.0)
    other_stream.wait_stream(default_stream)
    with other_stream:
        assert torch.cuda.current_stream(device) == other_stream.base_stream
        y = torch.sum(x)

    default_stream.wait_stream(other_stream)
    del x