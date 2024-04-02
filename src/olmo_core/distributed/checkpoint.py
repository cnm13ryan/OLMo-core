from __future__ import annotations

import json
import logging
import struct
import sys
import tempfile
from dataclasses import dataclass
from functools import cached_property, reduce
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, TypedDict

import safetensors as sft
import safetensors.torch as sft_torch
import torch
import torch.distributed as dist
import torch.nn as nn
from cached_path import cached_path
from pydantic import BaseModel

from olmo_core.exceptions import OLMoUserError
from olmo_core.io import (
    PathOrStr,
    clear_directory,
    deserialize_from_tensor,
    dir_is_empty,
    file_exists,
    get_bytes_range,
    is_url,
    serialize_to_tensor,
    upload,
)
from olmo_core.utils import TORCH_DTYPE_TO_STR, TORCH_DTYPES

from .sharded_flat_tensor import ShardedFlatTensor, ShardingSpec
from .utils import all_gather_object, barrier, get_rank, get_world_size, scatter_object

log = logging.getLogger(__name__)


@torch.no_grad()
def save_model_and_optim_state(
    dir: PathOrStr,
    model: nn.Module,
    optim: torch.optim.Optimizer,
    save_overwrite: bool = False,
) -> List[PathOrStr]:
    """
    Save model and optimizer state dictionaries. The model state can be a sharded model, in which
    case this method will correctly handle the optimizer state to ensure it can be loaded again with
    a different distributed topology through :func:`load_model_and_optim_state()`.

    Returns all of the files created by the current rank.
    """
    dir = str(dir).rstrip("/")

    # Ensure optimizer state has been initialized.
    init_optimizer_state(optim)

    model_state = _get_model_state_dict_for_checkpoint(model)
    flat_optim_state = _flatten_optimizer_state(
        model, optim, model_state, optim.state_dict()  # type: ignore[arg-type]
    )

    checkpointer = Checkpointer()
    _, model_files_created = checkpointer.save(f"{dir}/model", model_state, save_overwrite=save_overwrite)
    _, optim_files_created = checkpointer.save(f"{dir}/optim", flat_optim_state, save_overwrite=save_overwrite)
    return model_files_created + optim_files_created


@torch.no_grad()
def load_model_and_optim_state(
    dir: PathOrStr,
    model: nn.Module,
    optim: Optional[torch.optim.Optimizer] = None,
):
    """
    Load model and optimizer state in-place from a checkpoint saved via :func:`save_model_and_optim_state()`.
    This method is agnostic to the distributed topology in that it can load checkpoints saved with a different
    distributed topology.
    """
    dir = str(dir).rstrip("/")

    checkpointer = Checkpointer()

    # Get model state in-place.
    model_state = _get_model_state_dict_for_checkpoint(model)
    checkpointer.load(f"{dir}/model", model_state)
    _load_model_state_dict(model, model_state)

    if optim is not None:
        # Ensure optimizer state has been initialized.
        init_optimizer_state(optim)

        flat_optim_state = _flatten_optimizer_state(
            model, optim, model_state, optim.state_dict()  # type: ignore[arg-type]
        )
        del model_state

        # Make sure pickled fields are the right size.
        metadata = checkpointer.get_metadata(f"{dir}/optim")
        for i in range(len(optim.param_groups)):
            flat_optim_state[f"param_group{i}"] = metadata.tensors[f"param_group{i}"].materialize_empty()
        flat_optim_state["state_keys"] = metadata.tensors["state_keys"].materialize_empty()

        # Load flattened optimizer state in place.
        checkpointer.load(f"{dir}/optim", flat_optim_state, metadata=metadata)

        # Unflatten optimizer state and pass to optimizer.
        optim_state_to_load = _unflatten_optimizer_state(flat_optim_state)
        del flat_optim_state
        optim.load_state_dict(optim_state_to_load)  # type: ignore
        del optim_state_to_load


class Checkpointer:
    """
    A distributed checkpointer for saving and loading *non-nested* state dictionaries,
    i.e. where keys are strings and values are either regular :class:`torch.Tensor` instances,
    :class:`torch.nn.Parameter` instances, or any sharded tensors from this library.

    For saving and loading model and optimizer states together, use :func:`save_model_and_optim_state()`
    and :func:`load_model_and_optim_state()` instead.
    """

    METADATA_FILENAME = "metadata.json"

    @torch.no_grad()
    def save(
        self, dir: PathOrStr, state_dict: Dict[str, torch.Tensor], save_overwrite: bool = False
    ) -> Tuple[StorageMetadata, List[PathOrStr]]:
        """
        Save a state dict. The state dict can contain regular Tensors, Parameters, or any sharded tensors
        from this library.

        When calling this from a distributed context, all ranks must call this at the same time and the
        state dict must have the same keys and tensor types across each rank.

        Returns the storage metadata and a list of files created by the local rank.

        :param dir: The location to save the checkpoint to. Could be a path to a local directory or a URL
            to a "folder" in an S3 or GCS bucket.
        :param state_dict: The state dictionary to save.
        :param save_overwrite: Overwrite existing data.
        """
        dir = self._normalize_dir(dir)

        local_rank = get_rank()
        files_created: List[PathOrStr] = []

        local_dir: Path
        remote_dir: Optional[str] = None
        clean_up_local_dir = False
        if not is_url(dir):
            local_dir = Path(dir)
            if save_overwrite and not dir_is_empty(local_dir):
                clear_directory(local_dir)

            barrier()
            local_dir.mkdir(parents=True, exist_ok=True)
        else:
            local_dir = Path(tempfile.mkdtemp())
            remote_dir = str(dir).rstrip("/")
            clean_up_local_dir = True
            # NOTE: we do have the ability to clear bucket storage "folders" via `clear_directory`,
            # but that's super dangerous. All it takes is one person passing in the wrong folder
            # name and they could wipe out a ton of very important checkpoints.
            if not save_overwrite and file_exists(f"{remote_dir}/{self.METADATA_FILENAME}"):
                raise FileExistsError(f"Remote checkpoint directory '{remote_dir}' already contains a checkpoint!")

        try:
            if not dir_is_empty(local_dir):
                raise FileExistsError(f"Checkpoint directory '{local_dir}' is not empty!")

            barrier()

            flat_views, global_save_plan, metadata = self._get_global_save_plan_and_metadata(state_dict)

            # Construct local flat tensors state dict to save.
            local_state_dict: Dict[str, torch.Tensor] = {}
            for key in state_dict.keys():
                tensor_save_plan = global_save_plan.tensors[key]
                local_flat_tensor = flat_views[key]

                if (local_offsets := tensor_save_plan.flattened_offsets_per_rank.get(local_rank)) is not None:
                    assert local_offsets[1] - local_offsets[0] == local_flat_tensor.numel()
                    local_state_dict[key] = local_flat_tensor

            # Save safetensors file.
            local_sft_path = local_dir / self._filename_for_rank(local_rank)
            sft_torch.save_file(local_state_dict, local_sft_path)
            if remote_dir is not None:
                remote_sft_path = f"{remote_dir}/{self._filename_for_rank(local_rank)}"
                upload(
                    local_sft_path,
                    remote_sft_path,
                    save_overwrite=save_overwrite,
                )
                files_created.append(remote_sft_path)
            else:
                files_created.append(local_sft_path)

            # Save metadata.
            if local_rank == 0:
                local_metadata_path = local_dir / self.METADATA_FILENAME
                with open(local_metadata_path, "w") as f:
                    json.dump(metadata.model_dump(), f)

                if remote_dir is not None:
                    remote_metadata_path = f"{remote_dir}/{self.METADATA_FILENAME}"
                    upload(local_metadata_path, remote_metadata_path, save_overwrite=save_overwrite)
                    files_created.append(remote_metadata_path)
                else:
                    files_created.append(local_metadata_path)

            barrier()
        finally:
            if clean_up_local_dir and local_dir.exists():
                clear_directory(local_dir)

        return metadata, files_created

    @torch.no_grad()
    def load(
        self,
        dir: PathOrStr,
        state_dict: Dict[str, torch.Tensor],
        no_dist: bool = False,
        metadata: Optional[StorageMetadata] = None,
        _safetensors_mfl: Optional[SafeTensorsMultiFileLoader] = None,
    ):
        """
        Load a state dict in-place.

        :param dir: The path or URL to the checkpoint saved via :meth:`save()`.
        :param state_dict: The state dictionary to load into. This should contain all of the tensors
            you want to load.
        :param no_dist: Disable distributed communication even if within a distributed context.
        """
        dir = self._normalize_dir(dir)
        metadata = metadata or self.get_metadata(dir, no_dist=no_dist)
        safetensors_mfl = _safetensors_mfl or SafeTensorsMultiFileLoader()

        # Load each tensor from the slices in each file.
        for key in state_dict.keys():
            tensor_storage_metadata = metadata.tensors[key]
            tensor = state_dict[key]
            flat_view = self._get_flat_view(tensor)

            # Rank 0 will always be present, other ranks will not be for regular unsharded tensors.
            offsets: Tuple[int, int]
            if flat_view.is_sharded:
                offsets = flat_view.flattened_offsets_per_rank[get_rank()]
            else:
                if get_rank() in flat_view.flattened_offsets_per_rank:
                    offsets = flat_view.flattened_offsets_per_rank[get_rank()]
                else:
                    offsets = next(iter(flat_view.flattened_offsets_per_rank.values()))

            if flat_view.full_shape != tensor_storage_metadata.shape:
                raise ValueError(
                    f"Shape mismatched for '{key}', expected {flat_view.full_shape}, found {tensor_storage_metadata.shape}"
                )

            for filename, offsets_in_file in tensor_storage_metadata.flattened_offsets_per_file.items():
                # Check for overlap in offsets, and if there is overlap, load the slice from disk.
                if (
                    offsets_in_file[0] <= offsets[0] < offsets_in_file[1]
                    or offsets_in_file[0] < offsets[1] <= offsets_in_file[1]
                ):
                    with safetensors_mfl.open(f"{dir}/{filename}") as loader:
                        if len((shape_in_file := loader.get_shape(key))) != 1:
                            raise ValueError(
                                f"Expected a 1D tensor at {key} in {filename}, found shape {shape_in_file}"
                            )

                        if (dtype := loader.get_dtype(key)) != tensor.dtype:
                            raise ValueError(
                                f"Data type mismatch between tensor to load ({dtype}) and to load into ({tensor.dtype})"
                            )

                        numel_in_file = loader.get_numel(key)

                        # Start and end index of the slice within `flat_tensor` that we're going to load
                        # from a slice of `flat_tensor_to_load`.
                        flat_tensor_start, flat_tensor_end = 0, flat_view.view.numel()
                        # Start and end index of the slice within `flat_tensor_to_load` that we're going
                        # to load into the slice of `flat_tensor`.
                        flat_tensor_to_load_start, flat_tensor_to_load_end = 0, numel_in_file
                        # There are 5 scenarios to consider in terms of where the tensors overlap.
                        # Suppose the original flat tensor has 6 elements: 'x x x x x x'
                        # -------------------------------------------
                        # (A) flat_tensor_slice_to_load: [x x x]x x x  (0, 3)
                        #     flat_tensor:                x x[x x x]x  (2, 5)
                        # -------------------------------------------
                        # (B) flat_tensor_slice_to_load:  x x[x x x]x  (2, 5)
                        #     flat_tensor:               [x x x]x x x  (0, 3)
                        # -------------------------------------------
                        # (C) flat_tensor_slice_to_load:  x[x x x x]x  (1, 5)
                        #     flat_tensor:                x x[x x]x x  (2, 4)
                        # -------------------------------------------
                        # (D) flat_tensor_slice_to_load:  x x[x x]x x  (2, 4)
                        #     flat_tensor:                x[x x x x]x  (1, 5)
                        # -------------------------------------------
                        # (E) flat_tensor_slice_to_load:  x x[x x]x x  (2, 4)
                        #     flat_tensor:                x x[x x]x x  (2, 4)
                        # -------------------------------------------
                        if offsets[0] <= offsets_in_file[0]:
                            # Scenarios (B), (D), (E)
                            flat_tensor_start = offsets_in_file[0] - offsets[0]
                        else:
                            # Scenarios (A), (C)
                            flat_tensor_to_load_start = offsets[0] - offsets_in_file[0]

                        if offsets[1] <= offsets_in_file[1]:
                            # Scenarios (B), (C), (E)
                            flat_tensor_to_load_end -= offsets_in_file[1] - offsets[1]
                        else:
                            # Scenarios (A), (D)
                            flat_tensor_end -= offsets[1] - offsets_in_file[1]

                        log.debug(
                            "Loading '%s'\n  offsets: %s\n  offsets in file: %s\n  load into: (%s, %s)\n  load from: (%s, %s)",
                            key,
                            offsets,
                            offsets_in_file,
                            flat_tensor_start,
                            flat_tensor_end,
                            flat_tensor_to_load_start,
                            flat_tensor_to_load_end,
                        )

                        # Load the slice.
                        flat_tensor_to_load = loader.get_flat_slice(
                            key, flat_tensor_to_load_start, flat_tensor_to_load_end
                        )
                        if (
                            load_shape := flat_view.view[flat_tensor_start:flat_tensor_end].shape
                        ) != flat_tensor_to_load.shape:
                            raise RuntimeError(
                                f"error loading {key} from {filename} with offsets ({flat_tensor_start}, {flat_tensor_end}), "
                                f"expected {load_shape}, found {flat_tensor_to_load.shape}"
                            )
                        flat_view.view[flat_tensor_start:flat_tensor_end].copy_(flat_tensor_to_load)

                        del flat_tensor_to_load

            state_dict[key] = self._copy_into(tensor, flat_view.view)
            del flat_view

    @torch.no_grad()
    def unshard(
        self,
        dir: PathOrStr,
        device: Optional[torch.device] = None,
        rank0_only: bool = False,
        no_dist: bool = False,
    ) -> Dict[str, torch.Tensor]:
        """
        Unshard a checkpoint, returning the full state dict. This can be used in both distributed
        and non-distributed contexts. If you only want to load a single copy to rank 0 in a distributed
        context, set ``rank0_only=True``, in which case other ranks will receive an empty state dict.

        Alternatively, setting ``no_dist=True`` will return a full state dict from whatever process
        calls this.
        """
        dir = self._normalize_dir(dir)

        if rank0_only and no_dist and get_rank() != 0:
            raise ValueError(
                f"calling `unshard()` with `rank0_only=True` and `no_dist=True` is undefined for rank `{get_rank()} != 0`"
            )
        elif rank0_only and get_rank() != 0:
            return {}

        # Load metadata.
        metadata = self.get_metadata(dir, no_dist=no_dist or rank0_only)

        # Initialize state dict.
        state_dict = {}
        for key, tensor_metadata in metadata.tensors.items():
            state_dict[key] = tensor_metadata.materialize_empty(device=device)

        # Load the state dict in place.
        self.load(dir, state_dict, metadata=metadata, no_dist=no_dist or rank0_only)

        return state_dict

    def get_metadata(self, dir: str, no_dist: bool = False) -> StorageMetadata:
        """
        Get the storage metadata from a checkpoint directory.
        """
        dir = self._normalize_dir(dir)
        metadata: Optional[StorageMetadata] = None
        if no_dist or get_rank() == 0:
            with open(cached_path(f"{dir}/{self.METADATA_FILENAME}")) as f:
                metadata = StorageMetadata(**json.load(f))
        if not no_dist:
            metadata = scatter_object(metadata)
        assert metadata is not None
        return metadata

    def _filename_for_rank(self, rank: int) -> str:
        return f"rank_{rank}.safetensors"

    def _copy_into(self, target: torch.Tensor, source: torch.Tensor):
        target_data = _get_local_tensor_data(target)
        source_data = _get_local_tensor_data(source)
        target_data.copy_(source_data.view(target_data.shape))
        return target

    def _get_flat_view(self, tensor: torch.Tensor) -> TensorFlatView:
        full_shape: Tuple[int, ...]
        is_sharded: bool = False
        flattened_offsets_per_rank: Dict[int, Tuple[int, int]] = {}
        if isinstance(tensor, ShardedFlatTensor):
            full_shape = tensor.unsharded_shape
            is_sharded = True
            for pg_rank, offset in enumerate(tensor.sharding_spec.unsharded_flattened_offsets):
                # Translate process group rank into global rank.
                global_rank = (
                    pg_rank
                    if tensor.process_group is None
                    else dist.get_global_rank(tensor.process_group, pg_rank)
                )
                flattened_offsets_per_rank[global_rank] = offset
        else:
            full_shape = tuple(tensor.shape)
            flattened_offsets_per_rank = {get_rank(): (0, tensor.numel())}
        return TensorFlatView(
            view=_get_local_tensor_data(tensor).view(-1),
            full_shape=full_shape,
            is_sharded=is_sharded,
            flattened_offsets_per_rank=flattened_offsets_per_rank,
        )

    def _get_global_save_plan_and_metadata(
        self, state_dict: Dict[str, torch.Tensor]
    ) -> Tuple[Dict[str, torch.Tensor], SavePlan, StorageMetadata]:
        tensors_flat_view: Dict[str, torch.Tensor] = {}
        tensors_save_plan: Dict[str, TensorSavePlan] = {}
        tensors_metadata: Dict[str, TensorStorageMetadata] = {}
        for key, tensor in state_dict.items():
            flat_view = self._get_flat_view(tensor)
            tensors_flat_view[key] = flat_view.view
            tensors_save_plan[key] = TensorSavePlan(
                flattened_offsets_per_rank=flat_view.flattened_offsets_per_rank, is_sharded=flat_view.is_sharded
            )
            tensors_metadata[key] = TensorStorageMetadata(
                flattened_offsets_per_file={
                    self._filename_for_rank(rank): offsets
                    for rank, offsets in flat_view.flattened_offsets_per_rank.items()
                },
                shape=flat_view.full_shape,
                is_sharded=flat_view.is_sharded,
                dtype=TORCH_DTYPE_TO_STR[tensor.dtype],
            )

        # All-gather save plans across ranks, merge and validate.
        tensors_save_plan_all_ranks = all_gather_object(tensors_save_plan)
        final_tensors_save_plan: Dict[str, TensorSavePlan] = {}
        for rank_plan in tensors_save_plan_all_ranks:
            for key, plan in rank_plan.items():
                final_plan = final_tensors_save_plan.get(key)
                if final_plan is None:
                    final_tensors_save_plan[key] = plan
                elif plan != final_plan:
                    # TODO: handle case where a tensor is sharded in a process group, not globally.
                    if not plan.is_sharded and not final_plan.is_sharded:
                        # default to first rank with a save plan for this tensor
                        pass
                    elif not set(plan.flattened_offsets_per_rank).intersection(
                        final_plan.flattened_offsets_per_rank
                    ):
                        # tensor may be sharded in separate process groups, that's okay.
                        pass
                    else:
                        raise ValueError(
                            f"Save plan for '{key}' does not match across all ranks!\n"
                            f"1st plan: {final_plan}\n"
                            f"2nd plan: {plan}"
                        )

        # All-gather storage metadata across ranks, merge and validate.
        tensors_metadata_all_ranks = all_gather_object(tensors_metadata)
        final_tensors_metadata: Dict[str, TensorStorageMetadata] = {}
        for rank_metadata in tensors_metadata_all_ranks:
            for key, metadata in rank_metadata.items():
                final_metadata = final_tensors_metadata.get(key)
                if final_metadata is None:
                    final_tensors_metadata[key] = metadata
                elif metadata != final_metadata:
                    # TODO: handle case where a tensor is sharded in a process group, not globally.
                    if not metadata.is_sharded and not final_metadata.is_sharded:
                        # default to first rank with metadata for this tensor
                        pass
                    elif not set(metadata.flattened_offsets_per_file).intersection(
                        final_metadata.flattened_offsets_per_file
                    ):
                        # tensor may be sharded in separate process groups, that's okay.
                        pass
                    else:
                        raise ValueError(
                            f"Storage metadata for '{key}' does not match across all ranks!\n"
                            f"1st metadata: {final_tensors_metadata[key]}\n"
                            f"2nd metadata: {metadata}"
                        )

        return (
            tensors_flat_view,
            SavePlan(tensors=final_tensors_save_plan),
            StorageMetadata(tensors=final_tensors_metadata),
        )

    def _normalize_dir(self, dir: PathOrStr) -> str:
        dir = str(dir).strip("/")
        if dir.startswith("file://"):
            dir = dir.replace("file://", "", 1)
        return dir


class TensorStorageMetadata(BaseModel):
    flattened_offsets_per_file: Dict[str, Tuple[int, int]]
    """
    Maps file name to the offsets within the full flattened tensor that the shard in the file
    corresponds to.
    """

    shape: Tuple[int, ...]
    """
    The shape of the full (unflattened) tensor.
    """

    is_sharded: bool
    """
    Whether the original tensor (when saved) was sharded.
    """

    dtype: str
    """
    The data type of the tensor.
    """

    @property
    def torch_dtype(self) -> torch.dtype:
        return TORCH_DTYPES[self.dtype]

    def materialize_empty(
        self, *, device: Optional[torch.device] = None, shape: Optional[Tuple[int, ...]] = None
    ) -> torch.Tensor:
        return torch.empty(shape if shape is not None else self.shape, dtype=self.torch_dtype, device=device)

    def materialize_from_sharded(
        self, tensor: torch.Tensor, device: Optional[torch.device] = None
    ) -> torch.Tensor:
        if isinstance(tensor, ShardedFlatTensor):
            if tensor.unsharded_shape != self.shape:
                raise ValueError(
                    f"unexpected shape for sharded tensor, expected {self.shape}, got {tensor.unsharded_shape}"
                )
            return torch.empty(tensor.shape, device=device, dtype=self.torch_dtype)
        else:
            raise NotImplementedError(f"`materialize_from_sharded()` not implemented for {tensor}")


class StorageMetadata(BaseModel):
    tensors: Dict[str, TensorStorageMetadata]


class TensorSavePlan(BaseModel):
    flattened_offsets_per_rank: Dict[int, Tuple[int, int]]
    """
    Maps global process rank to the offsets within the full flattened tensor that the shard for the
    rank corresponds to. Some ranks may be omitted.
    """

    is_sharded: bool
    """
    If the tensor is sharded.
    """


@dataclass
class TensorFlatView:
    view: torch.Tensor
    """
    A 1D view into the tensor.
    """

    full_shape: Tuple[int, ...]
    """
    The shape of the full unsharded tensor.
    """

    is_sharded: bool
    """
    If the tensor is sharded.
    """

    flattened_offsets_per_rank: Dict[int, Tuple[int, int]]
    """
    A mapping of *global* rank to offsets into the full flattened tensor.
    """


class SavePlan(BaseModel):
    tensors: Dict[str, TensorSavePlan]


class SafeTensorsLoader:
    """
    A wrapper around ``safetensors`` loading functionality for PyTorch that works with remote
    files as well without having to download the whole file.

    This should be used a context manager.
    """

    def __init__(self, path: PathOrStr):
        self.path = path
        self.safe_open: Optional[sft.safe_open] = None

    @cached_property
    def header_length(self) -> int:
        return struct.unpack("<Q", get_bytes_range(self.path, 0, 8))[0]

    @cached_property
    def header(self) -> Dict[str, Any]:
        return json.loads(get_bytes_range(self.path, 8, self.header_length))

    def get_shape(self, key: str) -> Tuple[int, ...]:
        return self.header[key]["shape"]

    def get_dtype(self, key: str) -> torch.dtype:
        return sft_torch._getdtype(self.header[key]["dtype"])

    def get_numel(self, key: str) -> int:
        return reduce(lambda x, y: x * y, self.get_shape(key), 1)

    def get_flat_slice(self, key: str, start_idx: int = 0, end_idx: Optional[int] = None) -> torch.Tensor:
        if self.safe_open is not None:
            return self.safe_open.get_slice(key)[start_idx:end_idx]  # type: ignore
        elif is_url(self.path):
            # Validate indices. Can only work with positive indices.
            if start_idx < 0:
                start_idx = self.get_numel(key) + start_idx
            elif start_idx > self.get_numel(key):
                raise IndexError(f"slice start index ({start_idx}) out of range")

            if end_idx is None:
                end_idx = self.get_numel(key)
            elif end_idx < 0:
                end_idx = self.get_numel(key) + end_idx
            elif end_idx > self.get_numel(key):
                raise IndexError(f"slice end index ({end_idx}) out of range")

            dtype = self.get_dtype(key)
            bytes_per_item = sft_torch._SIZE[dtype]
            num_bytes = bytes_per_item * (end_idx - start_idx)
            if num_bytes == 0:
                return torch.tensor([], dtype=dtype)

            # Transform `start_idx` into a byte offset.
            offset_start = self.header[key]["data_offsets"][0]
            offset_start += bytes_per_item * start_idx
            # At this point `offset_start` is an offset into the byte-buffer part
            # of the file, not the file itself. We have to offset further by the header size byte
            # and the number of bytes in the header itself.
            offset_start += 8 + self.header_length

            # Load the tensor.
            array_bytes = get_bytes_range(self.path, offset_start, num_bytes)
            tensor = torch.frombuffer(bytearray(array_bytes), dtype=dtype)
            if sys.byteorder == "big":
                tensor = torch.from_numpy(tensor.numpy().byteswap(inplace=False))
            return tensor
        else:
            raise OLMoUserError(
                f"{self.__class__.__name__} is meant to be used as a context manager, did you forget to call __enter__?"
            )

    def __enter__(self):
        if not is_url(self.path):
            self.safe_open = sft.safe_open(self.path, framework="pt", device="cpu")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.safe_open is not None:
            self.safe_open.__exit__(exc_type, exc_val, exc_tb)  # type: ignore
            self.safe_open = None


class SafeTensorsMultiFileLoader:
    """
    A wrapper around :class:`SafeTensorsLoader` that should be used when working with multiple ``safetensors``
    files at once to avoid unnecessary IO.
    """

    def __init__(self):
        self.loaders: Dict[str, SafeTensorsLoader] = {}

    def open(self, path: PathOrStr) -> SafeTensorsLoader:
        if (loader := self.loaders.get(str(path))) is not None:
            return loader
        loader = SafeTensorsLoader(path)
        self.loaders[str(path)] = loader
        return loader


class ParamGroup(TypedDict):
    params: List[int]
    """
    Parameter IDs.
    """


class OptimStateDict(TypedDict):
    state: Dict[int, Dict[str, torch.Tensor]]
    """
    Maps parameter IDs to the optimizer-specific state of each parameter.
    """

    param_groups: List[ParamGroup]
    """
    Parameter groups.
    """


@torch.no_grad()
def init_optimizer_state(optim: torch.optim.Optimizer):
    """
    Ensure optimizer state is initialized for checkpointing.
    """
    if optim.state:
        return
    for group in optim.param_groups:
        for p in group["params"]:
            # Some parameters may be empty for sharded models, in which case the state does not need
            # to be initialized.
            if p.numel() > 0:
                p.grad = p.data.new(p.size()).zero_()
                p.grad.requires_grad_(False)
    optim.step()
    optim.zero_grad()


def _flatten_optimizer_state(
    model: nn.Module,
    optim: torch.optim.Optimizer,
    model_state: Dict[str, torch.Tensor],
    optim_state: OptimStateDict,
) -> Dict[str, torch.Tensor]:
    # Collect mapping of parameter IDs from the optimizer to the FQN of the corresponding parameter.
    param_id_to_name: Dict[int, str] = {}
    param_to_name: Dict[nn.Parameter, str] = {v: _patch_key(model, k) for k, v in model.named_parameters()}
    for param_group, param_group_state in zip(optim.param_groups, optim_state["param_groups"]):
        for param, param_id in zip(param_group["params"], param_group_state["params"]):
            param_id_to_name[param_id] = param_to_name[param]
    del param_to_name

    flat_optim_state: Dict[str, torch.Tensor] = {}

    # Serialize param groups to tensors.
    flat_optim_state["num_param_groups"] = torch.tensor(len(optim_state["param_groups"]))
    for i, param_group in enumerate(optim_state["param_groups"]):
        # make copy.
        param_group = {k: v for k, v in param_group.items()}
        param_group["_param_names"] = [param_id_to_name[param_id] for param_id in param_group["params"]]
        flat_optim_state[f"param_group{i}"] = serialize_to_tensor(param_group)

    # Flatten state tensors and wrap any tensor with the right sharded class if the corresponding
    # parameter is sharded.
    state_keys: Set[str] = set()
    for param_id, state in optim_state["state"].items():
        param_name = param_id_to_name[param_id]
        for key, tensor in state.items():
            state_keys.add(key)
            if key == "step" or tensor.shape != model_state[param_name].shape:
                # step tensors might be shared between params, which safetensors doesn't like, hence we clone
                tensor = tensor.clone()
            else:
                tensor = _wrap_tensor_for_sharded_parameter(tensor, model_state[param_name])
            flat_optim_state[_encode_state_key_for_param(param_name, key)] = tensor
    flat_optim_state["state_keys"] = serialize_to_tensor(sorted(state_keys))

    return flat_optim_state


def _unflatten_optimizer_state(flat_optim_state: Dict[str, torch.Tensor]) -> OptimStateDict:
    num_param_groups = int(flat_optim_state["num_param_groups"].item())
    optim_state: OptimStateDict = {
        "state": {},
        "param_groups": [],
    }

    param_name_to_id: Dict[str, int] = {}

    # Deserialize param group data while collecting the mapping of param names to IDs.
    for i in range(num_param_groups):
        param_group = deserialize_from_tensor(flat_optim_state[f"param_group{i}"])
        param_names = param_group.pop("_param_names")
        for param_name, param_id in zip(param_names, param_group["params"]):
            param_name_to_id[param_name] = param_id
        optim_state["param_groups"].append(param_group)

    # Unflatten the state tensors.
    state_keys = deserialize_from_tensor(flat_optim_state["state_keys"])
    for param_name, param_id in param_name_to_id.items():
        param_state: Dict[str, torch.Tensor] = {}
        for key in state_keys:
            state_tensor = flat_optim_state.get(_encode_state_key_for_param(param_name, key))
            if state_tensor is not None:
                # Ensure we have a regular tensor here, not some sharded wrapper.
                param_state[key] = _get_local_tensor_data(state_tensor)

        # Can'give pass the optimizer an empty state for a param.
        if param_state:
            optim_state["state"][param_id] = param_state

    return optim_state


def _state_key_prefix_for_param(param_name: str) -> str:
    return f"state.{param_name}.__"


def _encode_state_key_for_param(param_name: str, state_key: str) -> str:
    return f"{_state_key_prefix_for_param(param_name)}{state_key}"


@torch.no_grad()
def _get_model_state_dict_for_checkpoint(model: nn.Module) -> Dict[str, torch.Tensor]:
    from torch.distributed.fsdp import FullyShardedDataParallel as TorchFSDP

    if isinstance(model, TorchFSDP):
        return _get_torch_fsdp_state_dict_for_checkpoint(model)

    model_state = model.state_dict()
    key_to_param = {key: param for key, param in model.named_parameters()}
    for key, tensor in model_state.items():
        param = key_to_param.get(key)
        model_state[key] = _wrap_tensor_for_sharded_parameter(tensor, param)
    return model_state


@torch.no_grad()
def _get_torch_fsdp_state_dict_for_checkpoint(model: nn.Module) -> Dict[str, torch.Tensor]:
    from torch.distributed.fsdp import FullyShardedDataParallel as TorchFSDP
    from torch.distributed.fsdp._runtime_utils import _lazy_init

    assert isinstance(model, TorchFSDP)
    # Make sure FSDP initialization is complete, otherwise we can't access 'model._all_handles'.
    _lazy_init(model, model)

    param_to_flat_tensor: Dict[nn.Parameter, ShardedFlatTensor] = {}
    for handle in model._all_handles:
        if not handle.uses_sharded_strategy:
            continue

        if handle._use_orig_params:
            flat_param = handle.flat_param
            assert flat_param._params is not None
            for i, param in enumerate(flat_param._params):
                # Shape of the original parameter.
                og_shape = flat_param._shapes[i]

                # Offsets into the flattened original parameter.
                shard_info = flat_param._shard_param_infos[i]
                start_idx = shard_info.intra_param_start_idx
                end_idx = shard_info.intra_param_end_idx
                local_offsets = (
                    start_idx if start_idx is not None else 0,
                    end_idx + 1 if end_idx is not None else 0,
                )
                all_offsets: List[Tuple[int, int]] = [(0, 0)] * get_world_size(group=handle.process_group)
                all_offsets[get_rank(group=handle.process_group)] = local_offsets
                dist.all_gather_object(all_offsets, local_offsets, group=handle.process_group)

                # Wrap the parameter's data in a `ShardedFlatTensor`.
                shard_spec = ShardingSpec(
                    unsharded_shape=tuple(og_shape), unsharded_flattened_offsets=tuple(all_offsets)
                )
                flat_tensor = ShardedFlatTensor(param.data.detach())
                flat_tensor.mark_as_sharded(shard_spec)
                param_to_flat_tensor[param] = flat_tensor
        else:
            raise NotImplementedError(
                "checkpointing is only implemented for PyTorch FSDP with `use_orig_params=True`"
            )

    # Build state dict manually since `FSDP.state_dict()` does some nonsense.
    state_dict: Dict[str, torch.Tensor] = {}
    for name, param in model.named_parameters():
        name = _patch_key(model, name)
        state_dict[name] = param_to_flat_tensor.get(param, param.data.detach())

    # TODO: buffers

    return state_dict


@torch.no_grad()
def _load_model_state_dict(model: nn.Module, state_dict: Dict[str, torch.Tensor]):
    from torch.distributed.fsdp import FullyShardedDataParallel as TorchFSDP

    if isinstance(model, TorchFSDP):
        # We can't call `model.load_state_dict` directly on a TorchFSDP model because it does
        # some nonsense.
        _load_torch_fsdp_model_state_dict(model, state_dict)
    else:
        model.load_state_dict(state_dict)


@torch.no_grad()
def _load_torch_fsdp_model_state_dict(model: nn.Module, state_dict: Dict[str, torch.Tensor]):
    for name, param in model.named_parameters():
        name = name.replace("_fsdp_wrapped_module.", "")
        param.data.copy_(state_dict[name])


def _patch_key(model: nn.Module, key: str) -> str:
    from torch.distributed.fsdp import FullyShardedDataParallel as TorchFSDP

    if isinstance(model, TorchFSDP):
        return key.replace("_fsdp_wrapped_module.", "")
    else:
        return key


def _get_local_tensor_data(tensor: torch.Tensor) -> torch.Tensor:
    return tensor.data


def _wrap_tensor_for_sharded_parameter(tensor: torch.Tensor, param: Optional[torch.Tensor]) -> torch.Tensor:
    if isinstance(param, ShardedFlatTensor):
        return param.wrap(tensor, requires_grad=False)
    else:
        return tensor
