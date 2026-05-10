"""Constants for the Indygo Pool integration."""

import json
from logging import Logger, getLogger
from pathlib import Path

LOGGER: Logger = getLogger(__package__)

DOMAIN = "indygo_pool"
NAME = "Indygo Pool"
VERSION: str = json.loads((Path(__file__).parent / "manifest.json").read_text())[
    "version"
]

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_POOL_ID = "pool_id"

PROGRAM_TYPE_FILTRATION = 4

# Indygo input types (module.inputs[].type) -> what the input measures.
# Same mapping is used across all module types (ipx, lr-mas, lr-niv...).
INPUT_TYPE_TEMPERATURE = 5
INPUT_TYPE_PH = 6
INPUT_TYPE_ORP = 7
INPUT_TYPE_WATER_LEVEL = 33

# Indygo IPX output indices
IPX_OUTPUT_PH = 0           # outputs[0] -> pH related fields (pHSetpoint, pHMode)
IPX_OUTPUT_ELECTROLYSER = 1  # outputs[1] -> ORP/sel/electrolyser fields
