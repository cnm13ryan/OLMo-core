from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Dict

if TYPE_CHECKING:
    from ..trainer import Trainer


@dataclass
class Callback:
    """
    Trainer callback base class.

    Callbacks can be used to modify and extend the behavior of the trainer loop.
    This module contains a number of useful :class:`Callback` implementations, but you can
    always add your own.
    """

    priority: ClassVar[int] = 0
    """
    Priority of the callback. Determines the order in which callbacks run relative to each other.
    The higher the priority, the earlier a callback runs.
    """

    # NOTE: omegaconf can't use this annotation
    #  _trainer: Optional["Trainer"] = None
    _trainer = None

    @property
    def trainer(self) -> "Trainer":
        assert self._trainer is not None
        return self._trainer

    @trainer.setter
    def trainer(self, trainer: "Trainer"):
        self._trainer = trainer

    @property
    def step(self) -> int:
        return self.trainer.global_step

    #  def state_dict(self) -> Dict[str, Any]:
    #      return {}

    #  def load_state_dict(self, state_dict: Dict[str, Any]):
    #      del state_dict

    def pre_train(self):
        """
        Runs before the training loop starts.
        """
        pass

    def pre_epoch(self):
        """
        Runs before the start of a new epoch.
        """
        pass

    def pre_load_batch(self):
        """
        Runs right before the next batch is fetched from the data loader.
        """
        pass

    def pre_step(self, batch: Dict[str, Any]):
        """
        Runs right before a training batch is processed.
        """
        del batch

    def pre_optim_step(self):
        """
        Runs right after the forward-backward passes, right before the optimizer step.
        """
        pass

    def post_train_batch(self):
        """
        Runs after a training batch is processed.
        """
        pass

    def post_step(self):
        """
        Runs after a complete step (potentially including evals and checkpointing).
        """
        pass

    def log_metrics(self, step: int, metrics: Dict[str, float]):
        """
        Called when metrics have been gathered for a given step (possibly a previous step).
        """
        del step, metrics

    def post_epoch(self):
        """
        Runs at the end of a complete epoch.
        """
        pass

    def post_train(self):
        """
        Runs after the training loop successfully completes.
        """
        pass

    def on_error(self, exc: BaseException):
        """
        Called when the training loop exits with an error.
        """
        del exc