"""Core services. ``create_default_services`` builds them in dependency order."""

from __future__ import annotations

from typing import Optional


def create_default_services(rt, enabled: Optional[list[str]] = None) -> list:
    from .bag_service import BagService
    from .controller_service import ControllerService
    from .diagnostics_service import DiagnosticsService
    from .graph_service import GraphService
    from .health_service import HealthService
    from .interface_service import InterfaceService
    from .launch_service import LaunchService
    from .lifecycle_service import LifecycleService
    from .log_service import LogService
    from .param_service import ParamService
    from .robot_state_service import RobotStateService
    from .tf_service import TfService
    from .topic_service import TopicService
    from .utility_service import UtilityService

    classes = [GraphService, ParamService, TopicService, TfService, ControllerService,
               LifecycleService, LogService, DiagnosticsService, LaunchService, BagService,
               RobotStateService, HealthService, UtilityService, InterfaceService]
    if enabled is not None:
        classes = [c for c in classes if c.name in enabled]
    return [c(rt) for c in classes]


SERVICE_NAMES = ('graph', 'params', 'topics', 'tf', 'controllers', 'lifecycle', 'logs',
                 'diagnostics', 'launch', 'bags', 'robot', 'health', 'utility', 'interfaces')
