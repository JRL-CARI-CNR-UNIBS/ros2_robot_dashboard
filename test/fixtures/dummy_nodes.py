"""Test fixtures: nodes that exercise every dashboard service without real hardware.

They run in their own rclpy Context and executor thread, so a test process can host
both the fixtures and the dashboard runtime. Run standalone for manual tests:

    python3 test/fixtures/dummy_nodes.py
"""

from __future__ import annotations

import math
import threading
import time

import rclpy
from rclpy.context import Context
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.parameter import Parameter
from rcl_interfaces.msg import (FloatingPointRange, IntegerRange, ParameterDescriptor,
                                SetParametersResult)

URDF = """<?xml version="1.0"?>
<robot name="dummy_arm">
  <link name="base_link"/><link name="link1"/><link name="link2"/><link name="tool0"/>
  <joint name="joint1" type="revolute">
    <parent link="base_link"/><child link="link1"/>
    <limit lower="-3.14" upper="3.14" velocity="2.0" effort="100"/>
  </joint>
  <joint name="joint2" type="prismatic">
    <parent link="link1"/><child link="link2"/>
    <limit lower="0.0" upper="0.5" velocity="0.2" effort="50"/>
  </joint>
  <joint name="tool_joint" type="fixed">
    <parent link="link2"/><child link="tool0"/>
  </joint>
</robot>
"""


class ParamNode(Node):
    """Declares parameters of every type, with ranges, read-only, dynamic typing."""

    def __init__(self, context):
        super().__init__('dummy_params', namespace='/test', context=context)
        self.declare_parameter('flag', True)
        self.declare_parameter('count', 5, ParameterDescriptor(
            description='an integer with range',
            integer_range=[IntegerRange(from_value=0, to_value=10, step=1)]))
        self.declare_parameter('gain', 1.5, ParameterDescriptor(
            description='a double with range',
            floating_point_range=[FloatingPointRange(from_value=0.0, to_value=10.0, step=0.0)]))
        self.declare_parameter('name', 'robot')
        self.declare_parameter('joints', ['j1', 'j2', 'j3'])
        self.declare_parameter('weights', [1.0, 2.0, 3.0])
        self.declare_parameter('ids', [1, 2, 3])
        self.declare_parameter('bools', [True, False])
        self.declare_parameter('controller.gains.kp', 2.0)
        self.declare_parameter('controller.gains.ki', 0.1)
        self.declare_parameter('fixed', 42, ParameterDescriptor(
            read_only=True, description='read-only'))
        self.declare_parameter('dynamic', 1, ParameterDescriptor(dynamic_typing=True))
        self.declare_parameter('robot_description', URDF)
        self.add_on_set_parameters_callback(self._validate)

    def _validate(self, params):
        for p in params:
            if p.name == 'gain' and p.value == 6.66:
                return SetParametersResult(successful=False, reason='6.66 is forbidden')
        return SetParametersResult(successful=True)


class TopicsNode(Node):
    """Publishes joint states, a stamped pose, TF, diagnostics and logs."""

    def __init__(self, context):
        super().__init__('dummy_topics', namespace='/test', context=context)
        from diagnostic_msgs.msg import DiagnosticArray
        from geometry_msgs.msg import PoseStamped, TransformStamped
        from sensor_msgs.msg import JointState
        from std_msgs.msg import String
        from tf2_msgs.msg import TFMessage
        from rclpy.qos import DurabilityPolicy, QoSProfile

        self._js = self.create_publisher(JointState, '/joint_states', 10)
        self._pose = self.create_publisher(PoseStamped, '/test/pose', 10)
        self._tf = self.create_publisher(TFMessage, '/tf', 100)
        static_qos = QoSProfile(depth=10, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self._tf_static = self.create_publisher(TFMessage, '/tf_static', static_qos)
        self._diag = self.create_publisher(DiagnosticArray, '/diagnostics', 10)
        desc_qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self._desc = self.create_publisher(String, '/robot_description', desc_qos)
        self._desc.publish(String(data=URDF))
        self._sub = self.create_subscription(String, '/test/chatter', lambda m: None, 10)
        self._t0 = time.time()
        self._n = 0
        self._TransformStamped = TransformStamped
        self._TFMessage = TFMessage
        st = TransformStamped()
        st.header.frame_id = 'world'
        st.child_frame_id = 'base_link'
        st.transform.translation.z = 0.5
        st.transform.rotation.w = 1.0
        st.header.stamp = self.get_clock().now().to_msg()
        self._tf_static.publish(TFMessage(transforms=[st]))
        self.create_timer(0.02, self._tick)
        self.create_timer(0.5, self._diag_tick)

    def _tick(self):
        from geometry_msgs.msg import PoseStamped
        from sensor_msgs.msg import JointState
        t = time.time() - self._t0
        now = self.get_clock().now().to_msg()
        js = JointState()
        js.header.stamp = now
        js.name = ['joint1', 'joint2']
        js.position = [math.sin(t), 0.25 + 0.2 * math.sin(t)]
        js.velocity = [math.cos(t), 0.2 * math.cos(t)]
        js.effort = [0.0, 0.0]
        self._js.publish(js)
        p = PoseStamped()
        p.header.stamp = now
        p.header.frame_id = 'world'
        p.pose.position.x = t
        p.pose.orientation.w = 1.0
        self._pose.publish(p)
        tf = self._TransformStamped()
        tf.header.stamp = now
        tf.header.frame_id = 'base_link'
        tf.child_frame_id = 'tool0'
        tf.transform.translation.x = 0.3
        tf.transform.translation.z = js.position[1]
        yaw = js.position[0]
        tf.transform.rotation.z = math.sin(yaw / 2)
        tf.transform.rotation.w = math.cos(yaw / 2)
        self._tf.publish(self._TFMessage(transforms=[tf]))
        self._n += 1
        if self._n % 50 == 0:
            self.get_logger().warning(f'dummy warning {self._n}')

    def _diag_tick(self):
        from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
        arr = DiagnosticArray()
        arr.header.stamp = self.get_clock().now().to_msg()
        ok = DiagnosticStatus(name='dummy: motor', level=DiagnosticStatus.OK, message='running',
                              hardware_id='motor0', values=[KeyValue(key='temp', value='40')])
        warn = DiagnosticStatus(name='dummy: battery', level=DiagnosticStatus.WARN,
                                message='low', hardware_id='bat0')
        arr.status = [ok, warn]
        self._diag.publish(arr)


class FakeControllerManager(Node):
    """Implements the controller_manager services with in-memory controllers."""

    def __init__(self, context, namespace='/fake'):
        super().__init__('controller_manager', namespace=namespace, context=context)
        from controller_manager_msgs import srv
        self.controllers = {
            'joint_state_broadcaster': dict(type='joint_state_broadcaster/JointStateBroadcaster',
                                            state='active', cmd=[], st=['joint1/position']),
            'position_controller': dict(type='position_controllers/JointGroupPositionController',
                                        state='active', cmd=['joint1/position'],
                                        st=['joint1/position']),
            'trajectory_controller': dict(
                type='joint_trajectory_controller/JointTrajectoryController',
                state='inactive', cmd=['joint1/position'], st=['joint1/position']),
        }
        self.hw_state = ('active', 3)
        self.create_service(srv.ListControllers, '~/list_controllers', self._list)
        self.create_service(srv.ListHardwareInterfaces, '~/list_hardware_interfaces',
                            self._list_itfs)
        self.create_service(srv.ListHardwareComponents, '~/list_hardware_components',
                            self._list_comps)
        self.create_service(srv.SwitchController, '~/switch_controller', self._switch)
        self.create_service(srv.LoadController, '~/load_controller', self._load)
        self.create_service(srv.ConfigureController, '~/configure_controller', self._configure)
        self.create_service(srv.UnloadController, '~/unload_controller', self._unload)
        self.create_service(srv.SetHardwareComponentState, '~/set_hardware_component_state',
                            self._set_hw)

    def _list(self, req, res):
        from controller_manager_msgs.msg import ControllerState
        out = []
        for name, c in self.controllers.items():
            cs = ControllerState()
            cs.name, cs.type, cs.state = name, c['type'], c['state']
            cs.required_command_interfaces = c['cmd']
            cs.required_state_interfaces = c['st']
            cs.claimed_interfaces = c['cmd'] if c['state'] == 'active' else []
            if hasattr(cs, 'update_rate'):
                cs.update_rate = 100
            out.append(cs)
        res.controller = out
        return res

    def _claimed(self):
        return {i for c in self.controllers.values() if c['state'] == 'active' for i in c['cmd']}

    def _list_itfs(self, req, res):
        from controller_manager_msgs.msg import HardwareInterface
        claimed = self._claimed()
        res.command_interfaces = [HardwareInterface(name='joint1/position', is_available=True,
                                                    is_claimed='joint1/position' in claimed)]
        res.state_interfaces = [HardwareInterface(name='joint1/position', is_available=True,
                                                  is_claimed=False)]
        return res

    def _list_comps(self, req, res):
        from controller_manager_msgs.msg import HardwareComponentState
        hc = HardwareComponentState()
        hc.name, hc.type = 'FakeSystem', 'system'
        if hasattr(hc, 'plugin_name'):
            hc.plugin_name = 'mock_components/GenericSystem'
        if hasattr(hc, 'class_type'):
            hc.class_type = 'mock_components/GenericSystem'
        hc.state.label, hc.state.id = self.hw_state[0], self.hw_state[1]
        res.component = [hc]
        return res

    def _switch(self, req, res):
        strict = req.strictness == req.STRICT
        claimed = self._claimed()
        for name in req.deactivate_controllers:
            for i in self.controllers.get(name, {}).get('cmd', []):
                claimed.discard(i)
        ok = True
        msg = ''
        for name in req.activate_controllers:
            c = self.controllers.get(name)
            if c is None or (set(c['cmd']) & claimed):
                ok = False
                msg = f'cannot activate {name}'
        if ok or not strict:
            for name in req.deactivate_controllers:
                if name in self.controllers:
                    self.controllers[name]['state'] = 'inactive'
            if ok:
                for name in req.activate_controllers:
                    self.controllers[name]['state'] = 'active'
        res.ok = ok
        if hasattr(res, 'message'):
            res.message = msg
        return res

    def _load(self, req, res):
        self.controllers.setdefault(req.name, dict(type='dummy/Type', state='unconfigured',
                                                   cmd=[], st=[]))
        res.ok = True
        return res

    def _configure(self, req, res):
        c = self.controllers.get(req.name)
        res.ok = c is not None
        if c and c['state'] == 'unconfigured':
            c['state'] = 'inactive'
        return res

    def _unload(self, req, res):
        c = self.controllers.get(req.name)
        res.ok = c is not None and c['state'] != 'active'
        if res.ok:
            del self.controllers[req.name]
        return res

    def _set_hw(self, req, res):
        self.hw_state = (req.target_state.label, req.target_state.id)
        res.ok = True
        res.state.label, res.state.id = self.hw_state
        return res


def make_lifecycle_node(context):
    from rclpy.lifecycle import LifecycleNode, TransitionCallbackReturn

    class Managed(LifecycleNode):
        def on_configure(self, state):
            return TransitionCallbackReturn.SUCCESS

        def on_activate(self, state):
            return TransitionCallbackReturn.SUCCESS

        def on_deactivate(self, state):
            return TransitionCallbackReturn.SUCCESS

        def on_cleanup(self, state):
            return TransitionCallbackReturn.SUCCESS

    return Managed('dummy_lifecycle', namespace='/test', context=context)


class InterfacesNode(Node):
    """/test/dummy_interfaces: AddTwoInts service and Fibonacci action server."""

    def __init__(self, context):
        super().__init__('dummy_interfaces', namespace='/test', context=context)
        from example_interfaces.action import Fibonacci
        from example_interfaces.srv import AddTwoInts
        from rclpy.action import ActionServer, CancelResponse
        from rclpy.callback_groups import ReentrantCallbackGroup
        self._fib = Fibonacci
        group = ReentrantCallbackGroup()
        self.create_service(AddTwoInts, '/test/add_two_ints', self._add, callback_group=group)
        self.action = ActionServer(self, Fibonacci, '/test/fibonacci', self._execute,
                                   cancel_callback=lambda _g: CancelResponse.ACCEPT,
                                   callback_group=group)

    def _add(self, req, resp):
        resp.sum = req.a + req.b
        return resp

    def _execute(self, goal_handle):
        seq = [0, 1]
        fb = self._fib.Feedback()
        for _ in range(1, max(1, goal_handle.request.order)):
            if goal_handle.is_cancel_requested:
                goal_handle.canceled()
                return self._fib.Result(sequence=seq)
            seq.append(seq[-1] + seq[-2])
            fb.sequence = seq
            goal_handle.publish_feedback(fb)
            time.sleep(0.05)
        goal_handle.succeed()
        return self._fib.Result(sequence=seq)


class LoggerNode(Node):
    """/test/dummy_logger: logs at every level from its logger and a child logger.

    Created with ``enable_logger_service`` where available (Iron and newer).
    """

    def __init__(self, context):
        try:
            super().__init__('dummy_logger', namespace='/test', context=context,
                             enable_logger_service=True)
            self.has_logger_service = True
        except TypeError:            # Humble: no logger service
            super().__init__('dummy_logger', namespace='/test', context=context)
            self.has_logger_service = False
        self.child = self.get_logger().get_child('worker')
        self.count = 0
        self.create_timer(0.2, self._tick)

    def _tick(self):
        self.count += 1
        log = self.get_logger()
        log.debug(f'parent debug {self.count}')
        log.info(f'parent info {self.count}')
        self.child.debug(f'child debug {self.count}')
        self.child.info(f'child info {self.count}')


class DummySystem:
    """Starts all fixture nodes in a separate context and executor thread."""

    def __init__(self, with_lifecycle: bool = True):
        self.context = Context()
        rclpy.init(context=self.context)
        self.executor = MultiThreadedExecutor(num_threads=4, context=self.context)
        self.params = ParamNode(self.context)
        self.topics = TopicsNode(self.context)
        self.cm = FakeControllerManager(self.context)
        self.logger = LoggerNode(self.context)
        self.nodes = [self.params, self.topics, self.cm, self.logger]
        try:
            self.interfaces = InterfacesNode(self.context)
            self.nodes.append(self.interfaces)
        except ImportError:          # example_interfaces not installed
            self.interfaces = None
        if with_lifecycle:
            self.lifecycle = make_lifecycle_node(self.context)
            self.nodes.append(self.lifecycle)
        for n in self.nodes:
            self.executor.add_node(n)
        self._running = True
        self.thread = threading.Thread(target=self._spin, daemon=True)
        self.thread.start()

    def _spin(self):
        while self._running:
            try:
                self.executor.spin_once(timeout_sec=0.05)
            except Exception:  # noqa: BLE001
                if not self._running:
                    break

    def set_param(self, name, value):
        self.params.set_parameters([Parameter(name, value=value)])

    def shutdown(self):
        self._running = False
        self.thread.join(2.0)
        self.executor.shutdown()
        for n in self.nodes:
            n.destroy_node()
        rclpy.shutdown(context=self.context)


if __name__ == '__main__':
    import signal
    system = DummySystem()
    stop = threading.Event()
    # rclpy installs its own SIGINT handler: replace it so Ctrl+C ends the loop.
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    print('dummy nodes running, Ctrl+C to stop', flush=True)
    while not stop.wait(0.5):
        pass
    system.shutdown()
