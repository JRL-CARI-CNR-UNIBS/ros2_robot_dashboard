"""CompatLayer: the only place that knows about differences between ROS 2 distros."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from ..domain.bag import BagInfo, BagPlayConfig, BagRecordConfig
from ..domain.base import Serializable
from ..domain.control import ControllerInfo, HwComponentInfo, HwInterfaceInfo
from ..domain.graph import EndpointInfo


@dataclass(frozen=True)
class Capabilities(Serializable):
    """Feature flags used by the UI to hide or disable unsupported functions."""

    distro: str
    switch_returns_message: bool = False      # SwitchController.Response.message
    cleanup_controller: bool = False          # controller_manager/cleanup_controller
    controller_update_rate: bool = False      # ControllerState.update_rate
    hw_interface_data_type: bool = False      # HardwareInterface.data_type
    bag_storages: tuple[str, ...] = ('sqlite3', 'mcap')
    bag_default_storage: str = 'sqlite3'
    bag_record_services: bool = False         # ros2 bag record --services
    bag_play_delay: bool = False              # ros2 bag play --delay
    message_publisher_gid: bool = False
    extra: dict = field(default_factory=dict)


class CompatLayer(abc.ABC):
    """One implementation per distro. Service code never checks ROS_DISTRO itself."""

    @abc.abstractmethod
    def distro(self) -> str: ...

    @abc.abstractmethod
    def capabilities(self) -> Capabilities: ...

    # ----------------------------------------------------------------- graph
    @abc.abstractmethod
    def publishers_info(self, node, topic: str) -> list[EndpointInfo]: ...

    @abc.abstractmethod
    def subscriptions_info(self, node, topic: str) -> list[EndpointInfo]: ...

    @abc.abstractmethod
    def endpoint_from_rclpy(self, info, kind: str) -> EndpointInfo: ...

    # ----------------------------------------------------------------- ros2_control
    @abc.abstractmethod
    def controller_from_msg(self, msg) -> ControllerInfo: ...

    @abc.abstractmethod
    def hw_interface_from_msg(self, msg, kind: str) -> HwInterfaceInfo: ...

    @abc.abstractmethod
    def hw_component_from_msg(self, msg) -> HwComponentInfo: ...

    @abc.abstractmethod
    def make_switch_request(self, activate, deactivate, strict: bool, timeout_s: float,
                            activate_asap: bool = False) -> Any: ...

    @abc.abstractmethod
    def switch_result(self, response) -> tuple[bool, str]: ...

    # ----------------------------------------------------------------- bags
    @abc.abstractmethod
    def bag_record_command(self, config: BagRecordConfig, output_path: str) -> list[str]: ...

    @abc.abstractmethod
    def bag_play_command(self, config: BagPlayConfig) -> list[str]: ...

    @abc.abstractmethod
    def bag_info(self, path: str) -> BagInfo: ...

    # ----------------------------------------------------------------- messages
    @abc.abstractmethod
    def message_to_yaml(self, msg, truncate_length: int | None = None,
                        no_arr: bool = False) -> str: ...
