"""Contract tests: every CompatLayer gives the same observable results.

Message conversions use the messages of the distro the tests run on, so running the
suite on Humble, Jazzy and Lyrical checks each implementation against its real API.
"""

import os

import pytest

from ros2_robot_dashboard.core.compat import (IMPLEMENTATIONS, HumbleCompat, JazzyCompat,
                                              LyricalCompat, create_compat)
from ros2_robot_dashboard.core.domain.bag import BagPlayConfig, BagRecordConfig

ALL = [HumbleCompat, JazzyCompat, LyricalCompat]


def test_create_compat_selects_by_distro():
    assert isinstance(create_compat('humble'), HumbleCompat)
    assert isinstance(create_compat('jazzy'), JazzyCompat)
    assert isinstance(create_compat('lyrical'), LyricalCompat)
    assert isinstance(create_compat('rolling'), LyricalCompat)   # newest for unknown
    current = os.environ.get('ROS_DISTRO')
    if current in IMPLEMENTATIONS:
        assert create_compat().distro() == current


@pytest.mark.parametrize('cls', ALL)
def test_bag_record_command(cls):
    c = cls()
    cfg = BagRecordConfig(output_dir='/tmp', topics=('/a', '/b'), max_duration_s=60,
                          compression='zstd', exclude_regex='^/x')
    cmd = c.bag_record_command(cfg, '/tmp/out')
    assert cmd[:3] == ['ros2', 'bag', 'record']
    assert cmd[cmd.index('-o') + 1] == '/tmp/out'
    assert cmd[cmd.index('-s') + 1] == c.BAG_DEFAULT_STORAGE
    assert '-d' in cmd and '--compression-format' in cmd
    if cls is HumbleCompat:
        assert cmd[-3:] == ['--', '/a', '/b'] and '-x' in cmd
        assert '--disable-keyboard-controls' not in cmd
    else:
        i = cmd.index('--topics')
        assert cmd[i + 1:i + 3] == ['/a', '/b'] and '--exclude-regex' in cmd
    all_cmd = c.bag_record_command(BagRecordConfig('/tmp', all_topics=True), '/tmp/o')
    assert ('-a' if cls is HumbleCompat else '--all-topics') in all_cmd


@pytest.mark.parametrize('cls', ALL)
def test_bag_play_command(cls):
    cmd = cls().bag_play_command(BagPlayConfig('/tmp/bag', rate=2.0, loop=True, storage='mcap',
                                               topics=('/a',), clock=True))
    assert cmd[:4] == ['ros2', 'bag', 'play', '/tmp/bag']
    assert '-r' in cmd and '-l' in cmd and '--clock' in cmd
    assert ('-s' in cmd) == (cls is not LyricalCompat)


def test_bag_info_from_metadata(tmp_path):
    (tmp_path / 'metadata.yaml').write_text("""
rosbag2_bagfile_information:
  version: 5
  storage_identifier: mcap
  duration: {nanoseconds: 2500000000}
  starting_time: {nanoseconds_since_epoch: 1700000000000000000}
  message_count: 30
  topics_with_message_count:
    - topic_metadata: {name: /b, type: std_msgs/msg/String, serialization_format: cdr}
      message_count: 10
    - topic_metadata: {name: /a, type: std_msgs/msg/Int32, serialization_format: cdr}
      message_count: 20
  compression_format: ''
  relative_file_paths: [x_0.mcap]
""")
    (tmp_path / 'x_0.mcap').write_bytes(b'0' * 100)
    for cls in ALL:
        info = cls().bag_info(str(tmp_path))
        assert info.storage == 'mcap' and info.duration_s == 2.5 and info.message_count == 30
        assert [t.name for t in info.topics] == ['/a', '/b'] and info.size_bytes >= 100


@pytest.mark.ros
def test_controller_messages_current_distro():
    msg = pytest.importorskip('controller_manager_msgs.msg')
    c = create_compat()
    cs = msg.ControllerState(name='c', type='t', state='active',
                             claimed_interfaces=['j/position'],
                             required_command_interfaces=['j/position'])
    info = c.controller_from_msg(cs)
    assert info.name == 'c' and info.claimed_interfaces == ('j/position',)
    caps = c.capabilities()
    assert (info.update_rate is not None) == caps.controller_update_rate
    hc = msg.HardwareComponentState(name='hw', type='system')
    hc.state.id, hc.state.label = 3, 'active'
    comp = c.hw_component_from_msg(hc)
    assert comp.state_label == 'active' and comp.state_id == 3
    req = c.make_switch_request(['a'], ['b'], True, 2.5)
    assert list(req.activate_controllers) == ['a'] and req.timeout.sec == 2 \
        and req.timeout.nanosec == 500000000
    srv = pytest.importorskip('controller_manager_msgs.srv')
    res = srv.SwitchController.Response(ok=True)
    assert c.switch_result(res)[0] is True


@pytest.mark.ros
def test_message_to_yaml_current_distro():
    from std_msgs.msg import Float64MultiArray
    c = create_compat()
    m = Float64MultiArray(data=[float(i) for i in range(200)])
    full = c.message_to_yaml(m)
    short = c.message_to_yaml(m, truncate_length=5)
    assert len(short) < len(full) and '...' in short
