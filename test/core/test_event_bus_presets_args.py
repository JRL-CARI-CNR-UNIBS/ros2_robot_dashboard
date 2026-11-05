"""EventBus, presets, launch arguments, config (no ROS graph needed)."""

import threading
import time

import pytest

from ros2_robot_dashboard.core import presets
from ros2_robot_dashboard.core.config import Config, deep_merge
from ros2_robot_dashboard.core.domain.params import ParamDescriptorInfo, ParamInfo, ParamType
from ros2_robot_dashboard.core.domain.process import (ArgType, LaunchArg, ProcessProfile,
                                                      compose_launch_command)
from ros2_robot_dashboard.core.event_bus import EventBus
from ros2_robot_dashboard.core.futures import gather, then, completed, failed
from ros2_robot_dashboard.core.processes.launch_args import (check_condition, evaluate_condition,
                                                             parse_show_args)
from ros2_robot_dashboard.core.safety import ReadOnlyError, Safety
from ros2_robot_dashboard.core.services.launch_service import parse_profiles_yaml


# --------------------------------------------------------------------------- event bus

class A:
    def __init__(self, i):
        self.i = i


class B(A):
    pass


def test_bus_order_and_type_filter():
    bus = EventBus()
    bus.start()
    got_a, got_b = [], []
    bus.subscribe(A, lambda e: got_a.append(e.i))
    bus.subscribe(B, lambda e: got_b.append(e.i))
    for i in range(100):
        bus.publish(A(i) if i % 2 else B(i))
    assert bus.flush(2.0)
    assert got_a == list(range(100))
    assert got_b == list(range(0, 100, 2))
    bus.stop()


def test_bus_slow_subscriber_drops_but_others_continue():
    bus = EventBus()
    bus.start()
    fast = []
    gate = threading.Event()
    slow_sub = bus.subscribe(A, lambda e: gate.wait(0.5), max_queue=5)
    bus.subscribe(A, lambda e: fast.append(e.i))
    t0 = time.time()
    for i in range(50):
        bus.publish(A(i))
    assert time.time() - t0 < 0.1, 'publish must not block'
    gate.set()
    assert bus.flush(5.0)
    assert fast == list(range(50))
    assert slow_sub.dropped > 0
    bus.stop()


def test_bus_exceptions_do_not_break_delivery():
    bus = EventBus()
    errors = []
    bus.set_error_handler(lambda exc, sub, ev: errors.append(exc))
    bus.start()
    got = []

    def bad(e):
        raise RuntimeError('boom')

    bus.subscribe(A, bad)
    bus.subscribe(A, lambda e: got.append(e.i))
    for i in range(3):
        bus.publish(A(i))
    assert bus.flush(2.0)
    assert got == [0, 1, 2] and len(errors) == 3
    bus.stop()


def test_futures_helpers():
    assert then(completed(2), lambda v: v * 3).result() == 6
    assert gather([completed(1), completed(2)]).result() == [1, 2]
    with pytest.raises(ValueError):
        gather([completed(1), failed(ValueError('x'))]).result()


# --------------------------------------------------------------------------- presets

def _params():
    return [
        ParamInfo('update_rate', ParamType.INTEGER, 500),
        ParamInfo('controller.gains.kp', ParamType.DOUBLE, 1.5),
        ParamInfo('controller.joints', ParamType.STRING_ARRAY, ['j1', 'j2']),
        ParamInfo('ro', ParamType.INTEGER, 1, ParamDescriptorInfo(read_only=True)),
    ]


def test_preset_roundtrip_like_ros2_param_dump():
    text = presets.dump_yaml('/controller_manager', _params())
    assert 'ros__parameters' in text and 'ro:' not in text
    parsed = presets.parse_yaml(text)
    assert parsed == {'/controller_manager': {'update_rate': 500, 'controller.gains.kp': 1.5,
                                              'controller.joints': ['j1', 'j2']}}


def test_preset_wildcard_and_namespaced():
    text = """
/**:
  ros__parameters:
    use_sim_time: true
    update_rate: 100
ns:
  node:
    ros__parameters:
      update_rate: 250
"""
    parsed = presets.parse_yaml(text)
    assert presets.values_for_node(parsed, '/ns/node') == {'use_sim_time': True,
                                                           'update_rate': 250}
    assert presets.values_for_node(parsed, '/other') == {'use_sim_time': True,
                                                         'update_rate': 100}


def test_preset_diff_promotion_rules():
    current = [ParamInfo('d', ParamType.DOUBLE, 1.0), ParamInfo('i', ParamType.INTEGER, 1),
               ParamInfo('s', ParamType.STRING, 'a')]
    d = {x.name: x for x in presets.diff(current, {'d': 2, 'i': 2.5, 's': 'a', 'm': 3})}
    assert d['d'].preset == 2.0 and not d['d'].error          # int -> double promoted
    assert d['i'].error                                        # double -> int refused
    assert 's' not in d                                        # equal
    assert d['m'].missing


# --------------------------------------------------------------------------- launch args

SHOW_ARGS = """Arguments (pass arguments as '<name>:=<value>'):

    'ur_type':
        Type/series of used UR robot.
        Choices: ['ur3', 'ur3e', 'ur5e']

    'robot_ip':
        IP address by which the robot can be reached.

    'launch_rviz':
        Launch RViz?
        (default: 'true')

    'controller_file':
        YAML file with the controllers configuration.
        (default: LocalVar('FindPackageShare(pkg='ur_robot_driver') + 'config' + 'ur_controllers.yaml''))
"""


def test_parse_show_args():
    args = {a.name: a for a in parse_show_args(SHOW_ARGS)}
    assert args['ur_type'].type == ArgType.CHOICE and 'ur5e' in args['ur_type'].choices
    assert args['robot_ip'].required and args['robot_ip'].default is None
    assert args['launch_rviz'].type == ArgType.BOOL and args['launch_rviz'].default is True
    assert args['controller_file'].default.startswith('LocalVar')


def test_conditions_and_command():
    profile = ProcessProfile('p', 'P', 'ros2 launch cell cell.launch.py', args=(
        LaunchArg('sim', ArgType.BOOL, False),
        LaunchArg('robot_ip', ArgType.STRING, '10.0.0.1', enabled_if='not sim'),
        LaunchArg('controller', ArgType.CHOICE, 'velocity', choices=('position', 'velocity')),
        LaunchArg('rate', ArgType.INT, None),
    ))
    assert compose_launch_command(profile, {}) == [
        'ros2', 'launch', 'cell', 'cell.launch.py', 'sim:=false', 'robot_ip:=10.0.0.1',
        'controller:=velocity']
    cmd = compose_launch_command(profile, {'sim': True, 'rate': 5})
    assert 'robot_ip:=10.0.0.1' not in cmd and 'sim:=true' in cmd and 'rate:=5' in cmd
    assert evaluate_condition("controller == 'velocity' and not sim",
                              {'controller': 'velocity', 'sim': 'false'}, profile)
    assert check_condition('__import__("os")', {'sim'})
    assert check_condition('foo', {'sim'}).startswith('unknown')
    assert check_condition('not sim', {'sim'}) == ''


def test_parse_profiles_yaml_example():
    text = """
profiles:
  - id: cella
    name: Cella
    command: ros2 launch cella cella.launch.py
    env: {ROS_DOMAIN_ID: "7"}
    args:
      - {name: sim, type: bool, default: false}
      - {name: robot_ip, type: string, default: "192.168.0.10", enabled_if: "not sim"}
      - {name: controller, type: choice, choices: [position, velocity], default: velocity}
    presets:
      - {name: Sim, values: {sim: true}}
  - id: broken
    command: x
    args:
      - {name: a, type: bool, enabled_if: "missing_arg"}
scenarios:
  - id: avvia
    steps:
      - {profile: cella, preset: Sim, wait: {node: /controller_manager, timeout_s: 30}}
"""
    profiles, scenarios, errors = parse_profiles_yaml(text, 'x.yaml')
    assert [p.id for p in profiles] == ['cella']
    assert len(errors) == 1 and 'missing_arg' in errors[0]
    assert scenarios[0].steps[0].wait_node == '/controller_manager'
    assert profiles[0].env == {'ROS_DOMAIN_ID': '7'}


# --------------------------------------------------------------------------- config and safety

def test_config_merge_and_get(tmp_path):
    f = tmp_path / 'c.yaml'
    f.write_text('topics:\n  echo_max_rate_hz: 3\nnew_key: 1\n')
    cfg = Config.load(str(f), overrides={'read_only': True}, use_user_files=False)
    assert cfg.get('topics.echo_max_rate_hz') == 3
    assert cfg.get('topics.terminal_max_lines') == 5000
    assert cfg.get('read_only') is True and cfg.get('missing.key', 'd') == 'd'
    assert deep_merge({'a': {'b': 1}}, {'a': {'c': 2}}) == {'a': {'b': 1, 'c': 2}}


def test_safety_read_only_and_log(tmp_path):
    log = tmp_path / 'actions.log'
    s = Safety(read_only=True, log_path=str(log), risky_words=['stop', 'enable'],
               critical_params=['/cm:update_*'])
    with pytest.raises(ReadOnlyError):
        s.check_write('set_parameters', '/node')
    s.set_read_only(False)
    s.check_write('set_parameters', '/node')
    s.log('switch_controller', '/cm', True, 'detail')
    lines = log.read_text().splitlines()
    assert len(lines) == 3 and '"blocked: read-only mode"' in lines[0]
    assert s.is_risky_service('/robot/enable_motors') and not s.is_risky_service('/get_state')
    assert s.is_critical_param('/cm', 'update_rate')
