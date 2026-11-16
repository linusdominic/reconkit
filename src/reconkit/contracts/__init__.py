"""Data contracts: check a live table against a contract, and diff two contract versions."""

from reconkit.contracts.check import ContractResult, RuleResult, check_contract
from reconkit.contracts.diff import Change, diff_contracts
from reconkit.contracts.model import Contract, load_contract

__all__ = [
    "Change",
    "Contract",
    "ContractResult",
    "RuleResult",
    "check_contract",
    "diff_contracts",
    "load_contract",
]
