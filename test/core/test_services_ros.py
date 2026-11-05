"""Core services against the fixture nodes (needs rclpy; no Qt)."""

import os
import sys
import time

import pytest

from ros2_robot_dashboard.core.domain.events import (ControllerManagersChanged,
                                                     ControllerStateChanged, DiagnosticsUpdated,
                                                     EchoStopped, HostMetricsUpdated,
                                                     JointStatesUpdated, LifecycleNodesChanged,
                                                     LogRecordsBatch, ParamEvent, PlotSample,
                                                     ProcessStateChanged, RobotModelChanged,
                                                     TfTreeChanged, TopicSample, TopicStats,
                                                     TransformSampleEvent)
from ros2_robot_dashboard.core.domain.health import CriticalTopic, HealthLevel, HealthProfile
from ros2_robot_dashboard.core.domain.logs import DiagLevel, LogLevel
from ros2_robot_dashboard.core.domain.params import NodeStatus, ParamType
from ros2_robot_dashboard.core.domain.process import ProcessState
from ros2_robot_dashboard.core.futures import ServiceUnavailable
from ros2_robot_dashboard.core.safety import ReadOnlyError
from ros2_robot_dashboard.core.services.topic_service import EchoOptions, FileOptions

from conftest import wait_until

pytestmark = pytest.mark.ros
NODE = '/test/dummy_params'
CM = '/fake/controller_manager'


# --------------------------------------------------------------------------- graph

def test_graph_discovers_everything(runtime):
    snap = wait_until(lambda: (lambda s: s if s.topic('/test/pose') and any(
        a for a in s.services if a.name == f'{CM}/switch_controller') else None)(
        runtime.graph.poll()), 15)
    assert snap is not None
    names = {n.fqn for n in snap.nodes}
    assert {NODE, '/test/dummy_topics', CM} <= names
    pose = snap.topic('/test/pose')
    assert pose.type == 'geometry_msgs/msg/PoseStamped'
    assert pose.publishers[0].node_fqn == '/test/dummy_topics'
    assert pose.publishers[0].qos.reliability == 'reliable'
    itf = runtime.graph.node_interfaces('/test/dummy_topics')
    assert '/joint_states' in itf['publishers'] and '/test/chatter' in itf['subscribers']
    srv = next(s for s in snap.services if s.name == f'{NODE}/get_parameters')
    assert NODE in srv.servers
    assert not any(n.fqn.startswith('/robot_dashboard_') for n in snap.nodes)


# --------------------------------------------------------------------------- params

def test_params_load_types_and_descriptors(runtime):
    params = {p.name: p for p in runtime.params.load(NODE).result(10)}
    assert params['flag'].type == ParamType.BOOL and params['flag'].value is True
    assert params['count'].descriptor.int_range.to_value == 10
    assert params['gain'].descriptor.float_range is not None
    assert params['joints'].value == ['j1', 'j2', 'j3']
    assert params['fixed'].descriptor.read_only
    assert params['dynamic'].descriptor.dynamic_typing
    assert 'controller.gains.kp' in params
    assert runtime.params.status(NODE) == NodeStatus.READY


def test_params_set_reject_and_events(runtime, dummy_system, recorder):
    rec = recorder(ParamEvent)
    runtime.params.load(NODE).result(10)
    res = runtime.params.set(NODE, {'gain': 2.5, 'count': 7}).result(10)
    assert all(r.successful for r in res)
    rej = runtime.params.set(NODE, {'gain': 6.66}).result(10)
    assert not rej[0].successful and 'forbidden' in rej[0].reason
    out = runtime.params.set(NODE, {'count': 99}).result(10)   # outside range
    assert not out[0].successful
    atomic = runtime.params.set_atomic(NODE, {'controller.gains.kp': 3.0,
                                              'controller.gains.ki': 0.2}).result(10)
    assert all(r.successful for r in atomic)
    dyn = runtime.params.set(NODE, {'dynamic': 'now a string'}).result(10)
    assert dyn[0].successful
    # External change (like `ros2 param set`) arrives as an event.
    dummy_system.set_param('name', 'changed_outside')
    ev = rec.wait(ParamEvent, lambda e: e.param.name == 'name' and e.kind == 'changed')
    assert ev and ev[0].param.value == 'changed_outside'
    assert runtime.params.cached(NODE)['gain'].value == 2.5


def test_params_unreachable_node(runtime):
    with pytest.raises(ServiceUnavailable):
        runtime.params.load('/no/such_node').result(10)
    assert runtime.params.status('/no/such_node') == NodeStatus.UNREACHABLE


def test_read_only_blocks_writes(runtime):
    runtime.safety.set_read_only(True)
    try:
        with pytest.raises(ReadOnlyError):
            runtime.params.set(NODE, {'gain': 1.0}).result(5)
        with pytest.raises(ReadOnlyError):
            runtime.controllers.switch(CM, ['x'], []).result(5)
    finally:
        runtime.safety.set_read_only(False)


# --------------------------------------------------------------------------- topics

def test_echo_continuous_sampled(runtime, recorder):
    rec = recorder(TopicSample)
    eid = runtime.topics.start_echo('/joint_states', EchoOptions(max_rate_hz=5))
    time.sleep(2.0)
    runtime.topics.stop_echo(eid)
    samples = rec.of(TopicSample, lambda e: e.echo_id == eid)
    # 50 Hz publisher, 5 Hz sampling: ~10 samples in 2 s, with dropped counts.
    assert 4 <= len(samples) <= 14
    assert 'position:' in samples[-1].text and samples[-1].header_stamp
    assert sum(s.dropped for s in samples) > 20
    assert runtime.topics.subscription_count() == 0


def test_echo_once_and_file(runtime, recorder, tmp_path):
    rec = recorder(TopicSample, EchoStopped)
    eid = runtime.topics.start_echo('/test/pose', EchoOptions(once=True))
    assert rec.wait(EchoStopped, lambda e: e.echo_id == eid and e.reason == 'once')
    assert len(rec.of(TopicSample, lambda e: e.echo_id == eid)) == 1
    path = tmp_path / 'echo.yaml'
    eid2 = runtime.topics.start_echo('/test/pose', EchoOptions(max_rate_hz=2),
                                     file=FileOptions(str(path), max_messages=20))
    assert wait_until(lambda: path.exists() and path.read_text().count('---') >= 20, 10)
    runtime.topics.stop_echo(eid2)
    text = path.read_text()
    assert '# receive_time:' in text and '# header_stamp:' in text and 'position:' in text
    # every message is written, not only the sampled ones
    assert text.count('---') == 20


def test_topic_stats(runtime, recorder):
    rec = recorder(TopicStats)
    runtime.topics.start_stats('/joint_states', 'test')
    ev = rec.wait(TopicStats, lambda e: e.stats.topic == '/joint_states'
                  and e.stats.rate_hz > 0 and e.stats.latency_ms is not None, 10)
    runtime.topics.stop_stats('/joint_states', 'test')
    assert ev
    st = ev[-1].stats
    assert 30 < st.rate_hz < 70 and st.bandwidth_bps > 0 and st.latency_ms < 1000


def test_unknown_topic_errors(runtime):
    with pytest.raises(LookupError):
        runtime.topics.start_echo('/does/not/exist')


# --------------------------------------------------------------------------- tf

def test_tf_tree_and_echo(runtime, recorder):
    rec = recorder(TfTreeChanged, TransformSampleEvent)
    runtime.tf.acquire('test')
    ev = rec.wait(TfTreeChanged, lambda e: {'world', 'base_link', 'tool0'} <= set(e.tree.names()))
    assert ev
    tree = ev[-1].tree
    tool = next(f for f in tree.frames if f.name == 'tool0')
    assert tool.parent == 'base_link' and not tool.is_static
    assert next(f for f in tree.frames if f.name == 'base_link').is_static
    assert tree.roots() == ['world']
    eid = runtime.tf.start_echo('world', 'tool0', rate_hz=10)
    ok = rec.wait(TransformSampleEvent, lambda e: e.echo_id == eid and e.sample.ok)
    assert ok and abs(ok[-1].sample.translation[0] - 0.3) < 1e-6
    bad = runtime.tf.lookup('world', 'nonexistent')
    assert not bad.ok and bad.error
    runtime.tf.stop_echo(eid)
    runtime.tf.release('test')


# --------------------------------------------------------------------------- controllers

def test_controllers_discovery_list_and_switch(runtime, recorder):
    rec = recorder(ControllerStateChanged, ControllerManagersChanged)
    assert wait_until(lambda: CM in runtime.controllers.managers(), 10)
    runtime.controllers.watch(CM, 'test')
    ev = rec.wait(ControllerStateChanged, lambda e: e.manager == CM and e.controllers)
    assert ev
    state = ev[-1]
    by_name = {c.name: c for c in state.controllers}
    assert by_name['position_controller'].state == 'active'
    assert state.components and state.components[0].plugin == 'mock_components/GenericSystem'
    assert any(i.claimed_by == 'position_controller' for i in state.interfaces)
    # Conflict detected before calling the manager.
    plan = runtime.controllers.plan_switch(CM, ['trajectory_controller'], [])
    assert not plan.ok
    plan = runtime.controllers.plan_switch(CM, ['trajectory_controller'], ['position_controller'])
    assert plan.ok
    ok, _msg = runtime.controllers.switch(CM, ['trajectory_controller'],
                                          ['position_controller']).result(10)
    assert ok
    assert wait_until(lambda: {c.name: c.state for c in
                               runtime.controllers.last_state(CM).controllers}
                      .get('trajectory_controller') == 'active', 5)
    ok, _ = runtime.controllers.switch(CM, ['position_controller'], [], strict=True).result(10)
    assert not ok
    ok, _ = runtime.controllers.switch(CM, ['position_controller'],
                                       ['trajectory_controller']).result(10)
    assert ok
    assert runtime.controllers.load(CM, 'extra').result(10)[0]
    assert runtime.controllers.configure(CM, 'extra').result(10)[0]
    assert runtime.controllers.unload(CM, 'extra').result(10)[0]
    ok, msg = runtime.controllers.set_hardware_component_state(CM, 'FakeSystem',
                                                               'inactive').result(10)
    assert ok and 'inactive' in msg
    runtime.controllers.unwatch(CM, 'test')


# --------------------------------------------------------------------------- lifecycle

def test_lifecycle_transitions(runtime, recorder):
    rec = recorder(LifecycleNodesChanged)
    node = '/test/dummy_lifecycle'
    assert wait_until(lambda: node in runtime.lifecycle.nodes(), 10)
    runtime.lifecycle.watch('test')
    ev = rec.wait(LifecycleNodesChanged, lambda e: any(n.node == node for n in e.nodes))
    info = next(n for n in ev[-1].nodes if n.node == node)
    assert info.state_label == 'unconfigured'
    configure = next(t for t in info.transitions if t.label == 'configure')
    ok, _ = runtime.lifecycle.change_state(node, configure.id, 'configure').result(10)
    assert ok
    assert wait_until(lambda: runtime.lifecycle.refresh(node).result(5).state_label
                      == 'inactive', 5)
    runtime.lifecycle.unwatch('test')


# --------------------------------------------------------------------------- logs, diagnostics

def test_logs_and_diagnostics(runtime, recorder):
    rec = recorder(LogRecordsBatch, DiagnosticsUpdated)
    batch = rec.wait(LogRecordsBatch, lambda e: any('dummy warning' in r.message
                                                    for r in e.records), 10)
    assert batch
    recs = runtime.logs.records(min_level=LogLevel.WARN, nodes=['/test/dummy_topics'])
    assert recs and recs[-1].level == LogLevel.WARN and recs[-1].file
    assert runtime.logs.records(text='dummy w.*ning', regex=True)
    diag = rec.wait(DiagnosticsUpdated, lambda e: len(e.items) >= 2)
    items = {i.name: i for i in diag[-1].items}
    assert items['dummy: battery'].level == DiagLevel.WARN
    assert ('temp', '40') in items['dummy: motor'].values


# --------------------------------------------------------------------------- robot

def test_robot_state_and_model(runtime, recorder):
    rec = recorder(JointStatesUpdated, RobotModelChanged)
    runtime.robot.acquire('test')
    ev = rec.wait(JointStatesUpdated, lambda e: len(e.joints) == 2)
    assert ev and {j.name for j in ev[-1].joints} == {'joint1', 'joint2'}
    model = rec.wait(RobotModelChanged, lambda e: e.model is not None)
    assert model and model[-1].model.name == 'dummy_arm'
    # The parameter path also works (robot_description on any node).
    m = runtime.robot.load_description(NODE).result(10)
    assert m.joint('joint2').limits.upper == 0.5
    runtime.tf.acquire('robot-test')
    assert wait_until(lambda: runtime.robot.pose('world', 'tool0').ok, 5)
    runtime.tf.release('robot-test')
    runtime.robot.release('test')


# --------------------------------------------------------------------------- health, utility

def test_health_profile_and_hosts(runtime, recorder):
    rec = recorder(HostMetricsUpdated)
    profile = HealthProfile('t', 'test', (CriticalTopic('/joint_states', 50, 0.3),
                                          CriticalTopic('/never', 10)))
    runtime.health.set_custom_profile(profile)
    runtime.health.watch('test')
    res = wait_until(lambda: (lambda r: r if r and r[0].level == HealthLevel.OK else None)(
        runtime.health.evaluate()), 10)
    assert res and res[1].level == HealthLevel.ERROR
    hosts = rec.wait(HostMetricsUpdated)
    assert hosts and hosts[-1].hosts[0].local and hosts[-1].hosts[0].memory_percent > 0
    runtime.health.set_profile(None)
    runtime.health.unwatch('test')


def test_host_monitor_roundtrip():
    from ros2_robot_dashboard.core.services.host_metrics import (
        LocalMetrics, metrics_from_diagnostics, metrics_to_diagnostics)
    m = LocalMetrics('pc1').sample()
    back = metrics_from_diagnostics(metrics_to_diagnostics(m))
    assert back[0].host == 'pc1' and abs(back[0].memory_percent - m.memory_percent) < 0.01


def test_utility_search_compare_plot(runtime, recorder):
    runtime.params.load(NODE).result(10)
    kinds = {r.kind for r in runtime.utility.search('dummy')}
    assert {'node'} <= kinds
    assert any(r.kind == 'parameter' and r.name == 'controller.gains.kp'
               for r in runtime.utility.search('gains'))
    assert any(r.kind == 'topic' for r in runtime.utility.search('joint_states'))
    cmp = runtime.utility.compare_with_values(NODE, {'flag': True, 'name': 'other'}).result(10)
    by = {c.name: c for c in cmp}
    assert by['flag'].equal and not by['name'].equal and not by['count'].right_present
    assert 'pose.position.x' in runtime.utility.numeric_fields('/test/pose')
    rec = recorder(PlotSample)
    sid = runtime.utility.start_plot('/joint_states', 'position[0]', max_rate_hz=20)
    assert rec.wait(PlotSample, lambda e: e.source_id == sid, 5)
    runtime.utility.stop_plot(sid)


# --------------------------------------------------------------------------- launch and bags

def test_launch_profile_start_stop(runtime, recorder, tmp_path):
    from ros2_robot_dashboard.core.config import user_dir
    d = os.path.join(user_dir(), 'launch_profiles')
    os.makedirs(d, exist_ok=True)
    script = tmp_path / 'proc.py'
    script.write_text('import signal, sys, time\n'
                      'signal.signal(signal.SIGINT, lambda *a: (print("bye", flush=True), '
                      'sys.exit(0)))\n'
                      'print("args", sys.argv[1:], flush=True)\n'
                      'while True: time.sleep(0.1)\n')
    with open(os.path.join(d, 'test.yaml'), 'w') as f:
        f.write(f"""
profiles:
  - id: test_proc
    name: Test process
    command: {sys.executable} {script}
    args:
      - {{name: sim, type: bool, default: false}}
      - {{name: ip, type: string, default: "1.2.3.4", enabled_if: "not sim"}}
    stop: {{sigint_timeout_s: 3, sigterm_timeout_s: 2}}
""")
    runtime.launch.reload()
    assert 'test_proc' in [p.id for p in runtime.launch.profiles()]
    preview = runtime.launch.command_preview('test_proc', {'sim': True})
    assert preview[-1] == 'sim:=true'
    rec = recorder(ProcessStateChanged)
    st = runtime.launch.start_profile('test_proc', {'sim': False})
    assert st.state == ProcessState.RUNNING
    assert wait_until(lambda: any("'ip:=1.2.3.4'" in line for _, line in
                                  runtime.launch.output('test_proc')), 10)
    runtime.launch.restart_profile('test_proc', {'sim': True}).result(15)
    assert wait_until(lambda: any("['sim:=true']" in line for _, line in
                                  runtime.launch.output('test_proc')), 10)
    runtime.launch.stop_profile('test_proc').result(15)
    assert runtime.launch.status('test_proc').state == ProcessState.STOPPED
    assert rec.wait(ProcessStateChanged, lambda e: e.status.state == ProcessState.STOPPED)
    assert any(line == 'bye' for _, line in runtime.launch.output('test_proc'))
    path = runtime.launch.save_preset('test_proc', 'Sim', {'sim': True})
    assert os.path.isfile(path)
    assert runtime.launch.profile('test_proc').presets[0].values == {'sim': True}


def test_bag_record_and_info(runtime, tmp_path):
    from ros2_robot_dashboard.core.domain.bag import BagRecordConfig
    cfg = BagRecordConfig(output_dir=str(tmp_path), name='test', add_timestamp=False,
                          topics=('/test/pose',))
    rid = runtime.bags.record(cfg)
    time.sleep(4.0)
    st = runtime.bags.stop_record(rid).result(30)
    assert st.info is not None, st.message
    assert st.info.message_count > 10
    assert st.info.storage == runtime.compat.capabilities().bag_default_storage
    info = runtime.bags.info(str(tmp_path / 'test')).result(10)
    assert info.topics[0].name == '/test/pose'


def test_interfaces_service_call_and_action(runtime, dummy_system, recorder):
    if dummy_system.interfaces is None:
        pytest.skip('example_interfaces not installed')
    from ros2_robot_dashboard.core.services.interface_service import (
        ActionFeedback, ActionStatusChanged)
    assert wait_until(lambda: any(s.name == '/test/add_two_ints'
                                  for s in runtime.graph.poll().services), 10.0)
    res = runtime.interfaces.call('/test/add_two_ints', 'example_interfaces/srv/AddTwoInts',
                                  {'a': 2, 'b': 40}, timeout=5.0).result(10.0)
    assert res['sum'] == 42
    rec = recorder(ActionFeedback, ActionStatusChanged)
    assert wait_until(lambda: any(a.name == '/test/fibonacci'
                                  for a in runtime.graph.poll().actions), 10.0)
    gid = runtime.interfaces.send_goal('/test/fibonacci', 'example_interfaces/action/Fibonacci',
                                       {'order': 5})
    done = rec.wait(ActionStatusChanged, lambda e: e.goal_id == gid and e.status == 'succeeded',
                    15.0)
    assert done, rec.of(ActionStatusChanged)
    assert done[-1].result['sequence'] == [0, 1, 1, 2, 3, 5]
    assert rec.of(ActionFeedback, lambda e: e.goal_id == gid)
