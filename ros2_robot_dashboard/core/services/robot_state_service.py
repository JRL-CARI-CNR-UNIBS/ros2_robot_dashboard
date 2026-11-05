"""RobotStateService: joint states, limits from robot_description, poses from TF.

Robot-agnostic: the robot is discovered from ``joint_states``, ``robot_description``
(topic or parameter) and TF. No robot names in the code (design v2, D5/T3).
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from typing import Optional

from ..domain.events import JointStatesUpdated, RobotModelChanged
from ..domain.robot import JointState, RobotModel, parse_urdf
from ..futures import try_set_exception, try_set_result
from ..runtime import BaseService


class RobotStateService(BaseService):
    name = 'robot'

    def __init__(self, rt):
        super().__init__(rt)
        self._lock = threading.Lock()
        self._consumers: set[str] = set()
        self._joints: dict[str, JointState] = {}
        self._dirty = False
        self._topic = ''
        self._sub = None
        self._desc_sub = None
        self._timer = None
        self._model: Optional[RobotModel] = None

    def start(self) -> None:
        self._topic = self.config.get('robot.joint_states_topic', '/joint_states')

    def stop(self) -> None:
        self._teardown()

    # ----------------------------------------------------------------- consumers

    def acquire(self, consumer_id: str) -> None:
        with self._lock:
            self._consumers.add(consumer_id)
            first = self._sub is None
        if first:
            self._setup()

    def release(self, consumer_id: str) -> None:
        with self._lock:
            self._consumers.discard(consumer_id)
            empty = not self._consumers
        if empty:
            self._teardown()

    def set_joint_states_topic(self, topic: str) -> None:
        if topic == self._topic:
            return
        self._topic = topic
        with self._lock:
            self._joints.clear()
            active = self._sub is not None
        if active:
            self._teardown()
            self._setup()

    @property
    def joint_states_topic(self) -> str:
        return self._topic

    def _setup(self) -> None:
        from rclpy.qos import qos_profile_sensor_data
        from sensor_msgs.msg import JointState as JointStateMsg

        # Sensor-data QoS (best effort) matches both reliable and best-effort publishers.
        self._sub = self.rt.create_subscription(JointStateMsg, self._topic, self._on_joints,
                                                qos_profile_sensor_data,
                                                source='robot.joint_states')
        rate = float(self.config.get('robot.publish_rate_hz', 10.0))
        self._timer = self.rt.create_timer(1.0 / max(rate, 1.0), self._publish, 'robot.publish')
        self._subscribe_description()
        if self._model is None:
            self.load_description()

    def _teardown(self) -> None:
        self.rt.destroy_subscription(self._sub)
        self.rt.destroy_subscription(self._desc_sub)
        self.rt.destroy_timer(self._timer)
        self._sub = self._desc_sub = self._timer = None

    # ----------------------------------------------------------------- joint states

    def _on_joints(self, msg) -> None:
        n = len(msg.name)
        pos, vel, eff = list(msg.position), list(msg.velocity), list(msg.effort)
        with self._lock:
            for i, name in enumerate(msg.name):
                self._joints[name] = JointState(
                    name,
                    pos[i] if i < len(pos) else None,
                    vel[i] if i < len(vel) else None,
                    eff[i] if i < len(eff) else None)
            self._dirty = n > 0

    def _publish(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            self._dirty = False
            joints = tuple(self._joints[k] for k in sorted(self._joints))
        self.publish(JointStatesUpdated(topic=self._topic, joints=joints))

    def joints(self) -> list[JointState]:
        with self._lock:
            return [self._joints[k] for k in sorted(self._joints)]

    # ----------------------------------------------------------------- description

    def model(self) -> Optional[RobotModel]:
        return self._model

    def _subscribe_description(self) -> None:
        from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
        from std_msgs.msg import String

        topic = self.config.get('robot.robot_description_topic', '/robot_description')
        if not topic:
            return
        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                         durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self._desc_sub = self.rt.create_subscription(
            String, topic, lambda m: self._set_description(m.data, f'topic {topic}'), qos,
            source='robot.description')

    def _set_description(self, xml: str, source: str) -> None:
        try:
            model = parse_urdf(xml, source)
        except ValueError as exc:
            self.publish(RobotModelChanged(model=None, error=str(exc)))
            return
        self._model = model
        self.publish(RobotModelChanged(model=model))

    def load_description(self, node: Optional[str] = None) -> Future:
        """Read the ``robot_description`` parameter (default node from the config)."""
        out: Future = Future()
        node = node or self.config.get('robot.robot_description_node', '/robot_state_publisher')
        if not node:
            out.set_result(None)
            return out

        def done(f: Future) -> None:
            exc = f.exception()
            if exc is not None or not isinstance(f.result(), str) or not f.result():
                if self._model is None:
                    self.publish(RobotModelChanged(
                        model=None, error=f'robot_description not found on {node}: '
                        f'{exc or "empty"} (waiting for the topic)'))
                try_set_exception(out, exc or LookupError('robot_description is empty'))
                return
            self._set_description(f.result(), f'parameter {node}')
            try_set_result(out, self._model)

        self.rt.params.get_value(node, 'robot_description').add_done_callback(done)
        return out

    def set_description_xml(self, xml: str, source: str = 'file') -> None:
        self._set_description(xml, source)

    # ----------------------------------------------------------------- poses

    def pose(self, parent: str, child: str):
        """Latest TransformSample parent <- child (needs the TF service)."""
        return self.rt.tf.lookup(parent, child)

    def wait_for_joints(self, timeout: float = 5.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.joints():
                return True
            time.sleep(0.05)
        return False
