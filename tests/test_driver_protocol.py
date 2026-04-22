"""Tests for DriverProtocol abstract base."""

import pytest


class TestDriverProtocolIsABC:
    def test_cannot_instantiate_abstract_protocol(self):
        from drivers.driver_protocol import DriverProtocol

        with pytest.raises(TypeError):
            DriverProtocol()  # type: ignore[abstract]

    def test_subclass_must_implement_all_methods(self):
        from drivers.driver_protocol import DriverProtocol

        class Partial(DriverProtocol):
            async def start(self, engagement, prompt, options): ...
            # missing send_user_turn, cancel, resume, is_alive

        with pytest.raises(TypeError):
            Partial()  # type: ignore[abstract]
