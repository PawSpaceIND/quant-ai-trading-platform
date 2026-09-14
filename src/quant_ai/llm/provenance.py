"""Serializable, per-decision provenance; hashes identify content, not authenticity."""
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256


def normalize(value):
    if is_dataclass(value) and not isinstance(value, type):
        return normalize(asdict(value))
    if isinstance(value, (Decimal, datetime)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [normalize(item) for item in value]
    return value


def content_hash(value) -> str:
    data = json.dumps(normalize(value), sort_keys=True, separators=(',', ':'), allow_nan=False)
    return sha256(data.encode()).hexdigest()


class ConsensusPayload(dict):
    """Keep the strict tool payload unchanged while carrying this call's metadata."""
    def __init__(self, payload: dict, provenance: dict):
        super().__init__(payload)
        self.provenance = provenance
