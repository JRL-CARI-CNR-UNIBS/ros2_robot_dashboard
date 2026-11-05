"""Pure tests of domain types (no ROS needed)."""

import json
import math

import pytest

from ros2_robot_dashboard.core.domain import control, graph, params, robot, tf
from ros2_robot_dashboard.core.domain.events import CoreError, NodesChanged
from ros2_robot_dashboard.core.domain.health import CriticalTopic, HealthLevel, TopicStatsInfo, \
    evaluate_topic
from ros2_robot_dashboard.core.domain.params import ParamDescriptorInfo, ParamInfo, ParamType, \
    Range


# --------------------------------------------------------------------------- params

@pytest.mark.parametrize('text,ptype,expected', [
    ('true', ParamType.BOOL, True), ('0', ParamType.BOOL, False), ('42', ParamType.INTEGER, 42),
    ('0x10', ParamType.INTEGER, 16), ('1e-3', ParamType.DOUBLE, 0.001),
    ('hello', ParamType.STRING, 'hello'), ('1, 2, 3', ParamType.INTEGER_ARRAY, [1, 2, 3]),
    ('[1.5, 2]', ParamType.DOUBLE_ARRAY, [1.5, 2.0]),
    ('[a, "b, c", d]', ParamType.STRING_ARRAY, ['a', 'b, c', 'd']),
    ('[]', ParamType.STRING_ARRAY, []), ('true, false', ParamType.BOOL_ARRAY, [True, False]),
    ('1, 255', ParamType.BYTE_ARRAY, [1, 255]),
])
def test_coerce_text(text, ptype, expected):
    assert params.coerce_text(text, ptype) == expected


def test_coerce_errors_point_to_element():
    with pytest.raises(ValueError, match='element 2'):
        params.coerce_text('1.0, 2.0, x', ParamType.DOUBLE_ARRAY)
    with pytest.raises(ValueError):
        params.coerce_text('1.5', ParamType.INTEGER)
    with pytest.raises(ValueError, match='byte'):
        params.coerce_text('300', ParamType.BYTE_ARRAY)


def test_validate_ranges():
    info = ParamInfo('x', ParamType.INTEGER, 5,
                     ParamDescriptorInfo(int_range=Range(0, 10, 2)))
    assert params.validate(4, info) == (True, '')
    assert not params.validate(5, info)[0]       # not on step
    assert not params.validate(12, info)[0]
    finfo = ParamInfo('g', ParamType.DOUBLE, 1.0,
                      ParamDescriptorInfo(float_range=Range(0.0, 1.0, 0.0)))
    assert params.validate(0.5, finfo)[0]
    assert not params.validate(1.5, finfo)[0]
    ro = ParamInfo('r', ParamType.INTEGER, 1, ParamDescriptorInfo(read_only=True))
    assert not params.validate(1, ro)[0]


def test_infer_and_format():
    assert params.infer_type([1, 2]) == ParamType.INTEGER_ARRAY
    assert params.infer_type([1, 2.5]) == ParamType.DOUBLE_ARRAY
    assert params.infer_type(True) == ParamType.BOOL
    assert params.format_value([True, False], ParamType.BOOL_ARRAY) == '[true, false]'
    assert params.format_value(['a', 'b c,'], ParamType.STRING_ARRAY) == '[a, "b c,"]'
    assert params.values_equal(math.nan, math.nan)
    assert params.describe_range(ParamDescriptorInfo(int_range=Range(0, 10, 1))) == \
        '[0, 10, step 1]'


def test_msg_roundtrip():
    pytest.importorskip('rcl_interfaces')
    for value, ptype in [(True, ParamType.BOOL), (3, ParamType.INTEGER), (2.5, ParamType.DOUBLE),
                         ('s', ParamType.STRING), ([1, 2], ParamType.BYTE_ARRAY),
                         ([1.0, 2.0], ParamType.DOUBLE_ARRAY), (['a'], ParamType.STRING_ARRAY)]:
        msg = params.value_to_msg(value, ptype)
        assert params.param_type_from_msg(msg) == ptype
        assert params.value_from_msg(msg) == value


# --------------------------------------------------------------------------- serialization

def test_events_json_serializable():
    ev = NodesChanged(nodes=(graph.NodeInfo('a', '/ns'),), added=('/ns/a',))
    env = ev.envelope()
    assert env['type'] == 'event.NodesChanged'
    json.dumps(env)
    json.dumps(CoreError(source='x', message='y').to_dict())
    info = ParamInfo('p', ParamType.DOUBLE, math.inf)
    assert json.loads(json.dumps(info.to_dict()))['value'] == 'inf'


# --------------------------------------------------------------------------- graph

def test_names_and_qos_issues():
    assert graph.join_name('/', 'n') == '/n'
    assert graph.join_name('/ns', 'n') == '/ns/n'
    assert graph.split_name('/a/b/c') == ('/a/b', 'c')
    pub = graph.EndpointInfo('p', '/', 'std_msgs/msg/String', 'publisher',
                             qos=graph.QosInfo('best_effort', 'volatile', 'keep_last', 10))
    sub = graph.EndpointInfo('s', '/', 'std_msgs/msg/String', 'subscription',
                             qos=graph.QosInfo('reliable', 'transient_local', 'keep_last', 10))
    issues = graph.qos_issues(graph.TopicInfo('/t', ('std_msgs/msg/String',), (pub,), (sub,)))
    assert len(issues) == 1 and 'best_effort' in issues[0].reason \
        and 'transient_local' in issues[0].reason
    assert pub.qos.short() == 'BE/V/KL10'


# --------------------------------------------------------------------------- tf

def test_rpy_quaternion_roundtrip():
    for rpy in [(0.1, -0.2, 0.3), (math.pi / 2 - 1e-3, 0.0, 1.0), (0, 0, -3.0)]:
        q = tf.rpy_to_quaternion(*rpy)
        back = tf.quaternion_to_rpy(*q)
        assert all(abs(a - b) < 1e-9 for a, b in zip(rpy, back))
    m = tf.transform_matrix((1, 2, 3), tf.rpy_to_quaternion(0, 0, math.pi / 2))
    assert abs(m[0][1] + 1.0) < 1e-9 and m[0][3] == 1 and m[3] == [0, 0, 0, 1]
    s = tf.TransformSample('a', 'b', 0.0, (1, 2, 3), (0, 0, 0, 1))
    assert 'roll' in tf.format_rotation(s, tf.RotationFormat.RPY_DEG)
    assert len(tf.format_rotation(s, tf.RotationFormat.MATRIX).splitlines()) == 4


def test_analyze_tree_issues():
    now = 100.0
    recs = {
        'b': tf.FrameRecord('b', 'a', False, [99.0, 99.5, 100.0], 100.0, {'a'}),
        'c': tf.FrameRecord('c', 'b', True, [], 50.0, {'b', 'x'}),
        'z': tf.FrameRecord('z', 'y', False, [80.0], 80.0, {'y'}),
        'l1': tf.FrameRecord('l1', 'l2', True, [], 1.0, {'l2'}),
        'l2': tf.FrameRecord('l2', 'l1', True, [], 1.0, {'l1'}),
    }
    snap = tf.analyze_tree(recs, now)
    kinds = {i.kind for i in snap.issues}
    assert {'multiple_roots', 'multiple_parents', 'stale', 'loop'} <= kinds
    b = next(f for f in snap.frames if f.name == 'b')
    assert abs(b.rate_hz - 2.0) < 1e-6
    assert 'a' in snap.roots()


# --------------------------------------------------------------------------- control

def test_plan_switch_conflicts():
    cs = [control.ControllerInfo('pos', 't', 'active', ('j1/position',), ('j1/position',)),
          control.ControllerInfo('traj', 't', 'inactive', (), ('j1/position',)),
          control.ControllerInfo('vel', 't', 'inactive', (), ('j1/velocity',))]
    plan = control.plan_switch(cs, ['traj'], [])
    assert not plan.ok and plan.conflicts[0].held_by == 'pos'
    plan = control.plan_switch(cs, ['traj'], ['pos'])
    assert plan.ok and ('j1/position', 'pos') in plan.released \
        and ('j1/position', 'traj') in plan.acquired
    plan = control.plan_switch(cs, ['vel', 'nope'], [])
    assert plan.ok and any('unknown' in w for w in plan.warnings)


# --------------------------------------------------------------------------- robot

URDF = """<robot name="r"><link name="a"/><link name="b"/><link name="c"/>
<joint name="j1" type="revolute"><parent link="a"/><child link="b"/>
<limit lower="-1" upper="1" velocity="2" effort="3"/></joint>
<joint name="j2" type="continuous"><parent link="b"/><child link="c"/></joint></robot>"""


def test_parse_urdf_and_limits():
    m = robot.parse_urdf(URDF)
    assert m.name == 'r' and [j.name for j in m.joints] == ['j1', 'j2']
    j1 = m.joint('j1')
    assert j1.limits.lower == -1 and j1.is_rotational
    assert robot.limit_ratio(j1, 0.0) == 0.5
    assert robot.near_limit(j1, 0.99)
    assert robot.limit_ratio(m.joint('j2'), 1.0) is None
    assert m.root_links() == ['a'] and m.tip_links() == ['c']
    with pytest.raises(ValueError):
        robot.parse_urdf('<notrobot/>')


# --------------------------------------------------------------------------- health

def test_evaluate_topic():
    spec = CriticalTopic('/t', expected_hz=100, tolerance=0.2, max_latency_ms=10)
    assert evaluate_topic(spec, None).level == HealthLevel.ERROR
    ok = TopicStatsInfo('/t', 99.0, 1000, 5.0, 100, 0.01)
    assert evaluate_topic(spec, ok).level == HealthLevel.OK
    slow = TopicStatsInfo('/t', 70.0, 1000, 5.0, 100, 0.01)
    assert evaluate_topic(spec, slow).level == HealthLevel.WARN
    dead = TopicStatsInfo('/t', 99.0, 1000, 5.0, 100, 5.0)
    assert evaluate_topic(spec, dead).level == HealthLevel.ERROR
    late = TopicStatsInfo('/t', 99.0, 1000, 50.0, 100, 0.01)
    assert evaluate_topic(spec, late).level == HealthLevel.WARN
