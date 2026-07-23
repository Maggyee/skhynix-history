from dataclasses import dataclass

@dataclass
class DownloadResult:
    metadata: dict
    prices: list[dict]
    funding: list[dict]
    errors: list[str]

