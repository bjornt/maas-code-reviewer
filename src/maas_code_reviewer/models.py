# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class MergeProposal:
    url: str
    api_url: str
    source_git_repository: str
    source_git_path: str
    target_git_repository: str
    target_git_path: str
    status: str
    commit_message: str | None
    description: str | None
    _lp_object: Any = field(compare=False, hash=False, repr=False)


@dataclass(frozen=True)
class Comment:
    author: str
    body: str
    date: datetime
