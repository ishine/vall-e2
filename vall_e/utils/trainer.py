"""
# https://github.com/enhuiz/pytorch-training-utilities
"""

import humanize
import json
import logging
import numpy as np
import random
import selectors
import sys
import torch
import os

from functools import cache
from torch.distributed import broadcast_object_list
from torch.utils.data import DataLoader
from tqdm import tqdm
from typing import Protocol

from ..config import cfg
from .distributed import init_distributed, distributed_initialized, world_size
from .distributed import (
	global_leader_only,
	global_rank,
	is_global_leader,
	is_local_leader,
	local_leader_only,
)

from ..engines import _Engine, Engine, Engines, TrainFeeder, default_feeder, load_engines

from .utils import to_device, do_gc, truncate_json
from ..utils import wrapper as ml
from ..data import get_phone_symmap # should decouple from this trainer script

_logger = logging.getLogger(__name__)
_command: str


class EvalFn(Protocol):
	def __call__(self, *, engines: Engines):
		...


class Logger(Protocol):
	def __call__(self, *, data: dict):
		...


@cache
def _get_stdin_selector():
	selector = selectors.DefaultSelector()
	selector.register(fileobj=sys.stdin, events=selectors.EVENT_READ)
	return selector


if os.name == "nt":
	import msvcrt
	_buffer = []

def _non_blocking_input():
	global _command
	global _buffer
	l = [""]

	def _windows():
		global _buffer

		if msvcrt.kbhit():
			s: str = msvcrt.getch().decode('utf-8')
			if s == '\r':
				s = "".join(_buffer)
				_buffer = []
				return s

			_buffer.append(s)
		return ""

	def _linux():
		s = ""
		selector = _get_stdin_selector()
		events = selector.select(timeout=0)
		for key, _ in events:
			s: str = key.fileobj.readline().strip()
		return s

	if is_global_leader():
		s = _windows() if os.name == 'nt' else _linux()
	
		if s != "":
			_logger.info(f'Get stdin "{s}".')

		l[0] = s

	if world_size() > 1:
		broadcast_object_list(l, src=0)
	_command = l[0]
	return _command


def _make_infinite_epochs(dl):
	while True:
		#_logger.info("New epoch starts.")
		yield from tqdm(dl, "Epoch progress", dynamic_ncols=True, disable=not is_global_leader())


@local_leader_only(default=None)
def logger(data):
	return _logger.info(json.dumps(data, default=str))


def seed(seed):
	# Set up random seeds, after fork()
	random.seed(seed + global_rank())
	np.random.seed(seed + global_rank())
	torch.manual_seed(seed + global_rank())


def train(
	train_dl: DataLoader,
	train_feeder: TrainFeeder = default_feeder,
	eval_fn: EvalFn = lambda x: ...,
	logger: Logger = logger,
):
	engines = load_engines()

	"""
	if is_local_leader():
		cfg.dump()
		_logger.info(cfg)
	"""

	# Setup global engines
	global _engines
	_engines = engines

	events = []

	eval_fn = global_leader_only(eval_fn)

	# Pre-loop command
	command = _non_blocking_input()
	if command in ["eval", "eval_quit"]:
		eval_fn(engines=engines)

	if command in ["quit", "eval_quit"]:
		return

	last_save_step = engines.global_step
	last_eval_step = 0

	"""
	if cfg.distributed:
		train_dl.sampler.set_epoch(int(engines.global_samples / len(train_dl.dataset.paths)))
	"""

	# Training loop
	for batch in _make_infinite_epochs(train_dl):
		if engines.global_step >= cfg.trainer.iterations:
			break

		#batch = to_device(batch, torch.cuda.current_device())
		stats = engines.step(batch=batch, feeder=train_feeder)
		stats['epoch'] = engines.global_samples / len(train_dl.dataset.paths)

		"""
		stats['batch'] = {
			'size': len(batch['text']),
			'id': batch['spkr_id'],
			'index': [ index for index in batch['index'] ],
			'text_len': [ text.shape[0] for text in batch['text'] ],
			'prom_len': [ prom.shape[0] for prom in batch['proms'] ],
			'resp_len': [ resp.shape[0] for resp in batch['resps'] ],
		}
		"""


		elapsed_time = stats.get("elapsed_time", 0)
		try:
			metrics = json.dumps(stats)
		except Exception as e:
			metrics = str(stats)

		if cfg.trainer.no_logger:
			tqdm.write(f"Training Metrics: {truncate_json(metrics)}.")
		else:
			_logger.info(f"Training Metrics: {truncate_json(metrics)}.")

		command = _non_blocking_input()

		if "@" in command:
			what, when = command.split("@")
			try:
				events.append((what, int(when)))
				_logger.info(f"Event {command} registered.")
			except Exception as e:
				_logger.error(e)
			command = ""

		# Commands are the current command plus the triggered (i.e. iteration >= trigger point) events
		events = [e for e in events if e[1] >= engines.global_step]
		commands = [command] + [e[0] for e in events if e[1] == engines.global_step]

		for command in commands:
			if command in ["event show", "event"]:
				msg = "Events:\n" + "\n".join(["@".join(map(str, e)) for e in events])
				_logger.info(msg)

			if command == "event clear":
				events.clear()

			if "time" in command:
				target_iter = cfg.trainer.iterations
				if " to " in command:
					try:
						target_iter = int(command.split(" to ")[-1])
					except Exception as e:
						_logger.error(e)
				remaining_iters = target_iter - engines.global_step + 1
				remaining_time = int(remaining_iters * elapsed_time)
				_logger.info(humanize.precisedelta(remaining_time))

			if "lr" in command:
				rate = float(command.split(" ")[-1])
				try:
					engines.set_lr(rate)
					print("Updating LR to:", rate)
				except Exception as e:
					print("Failed to set LR rate to:", rate, str(e))

			if "export" in command:
				train_dl.dataset.save_state_dict(cfg.relpath / f"sampler.rank{global_rank()}.pt")
				engines.save_checkpoint()
				last_save_step = engines.global_step

				if is_global_leader():
					engines.export(userdata={"symmap": get_phone_symmap()})

			save_ckpt_every = cfg.trainer.save_frequency or cfg.evaluation.frequency

			saving_commands = ["save"]
			export_commands = ["export"]

			if cfg.trainer.save_on_quit:
				saving_commands.append("quit")

			if cfg.trainer.export_on_quit:
				export_commands.append("quit")

			if cfg.trainer.export_on_save:
				export_commands.append("save")

			if engines.global_step != last_save_step:
				if engines.global_step % save_ckpt_every == 0 or command in saving_commands:
					train_dl.dataset.save_state_dict(cfg.relpath / f"sampler.rank{global_rank()}.pt")
					engines.save_checkpoint()
					last_save_step = engines.global_step
					
					if command in export_commands and is_global_leader():
						engines.export(userdata={"symmap": get_phone_symmap()})

			if engines.global_step != last_eval_step:
				if engines.global_step % cfg.evaluation.frequency == 0 or command in ["eval"]:
					last_eval_step = engines.global_step
					eval_fn(engines=engines)

			if command in ["quit"]:
				return
