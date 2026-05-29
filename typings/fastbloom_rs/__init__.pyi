from collections.abc import Sequence

class BloomFilter:
    def __init__(
        self,
        expected_elements: int,
        false_positive_probability: float = 0.01,
    ) -> None: ...
    def add_bytes(self, element: bytes) -> None: ...
    def contains_bytes(self, element: bytes) -> bool: ...
    def contains_bytes_batch(
        self,
        elements: Sequence[bytes],
        check_type: bool = True,
    ) -> Sequence[bool]: ...
