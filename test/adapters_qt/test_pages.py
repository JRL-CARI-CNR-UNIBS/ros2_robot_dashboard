"""Qt adapter tests (offscreen): every page opens and the main interactions work.

Uses the dummy fixture nodes and the shared runtime. Dialogs are not shown: the
confirmations are disabled through the config, ``confirm`` is patched where needed.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip('PySide6')
pytestmark = [pytest.mark.qt, pytest.mark.ros]

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from ros2_robot_dashboard.adapters.qt.main_window import PAGES, MainWindow  # noqa: E402


def pump(qtbot, predicate, timeout_ms: int = 10000):
    qtbot.waitUntil(lambda: bool(predicate()), timeout=timeout_ms)


@pytest.fixture
def window(qtbot, runtime, tmp_path, monkeypatch):
    # No modal dialog may block an offscreen test.
    monkeypatch.setattr(QMessageBox, 'exec', lambda self: QMessageBox.Cancel)
    import ros2_robot_dashboard.adapters.qt.widgets.common as common
    monkeypatch.setattr(common, 'show_error', lambda *a, **k: None)
    import ros2_robot_dashboard.adapters.qt.pages.base_page as base_page
    monkeypatch.setattr(base_page, 'show_error', lambda *a, **k: None)
    runtime.config.set('confirm_actions', False)
    runtime.safety.set_read_only(False)
    settings = QSettings(str(tmp_path / 'settings.ini'), QSettings.IniFormat)
    w = MainWindow(runtime, settings, initial_page='params')
    qtbot.addWidget(w)
    w.show()
    yield w
    for p in w._pages.values():
        p.shutdown()
    w.bridge.close()


def test_every_page_opens(window, qtbot):
    for pid, *_ in PAGES:
        window.navigate(pid)
        qtbot.wait(150)
        assert window.current_page_id() == pid
        assert pid in window._pages, window.page_errors.get(pid)
    assert window.nodes_label.text().endswith('nodes')


def _select_node(page, qtbot, node):
    pump(qtbot, lambda: page.node_model.row_of(node) >= 0)
    row = page.node_model.row_of(node)
    page.node_view.setCurrentIndex(page.node_proxy.mapFromSource(page.node_model.index(row)))
    pump(qtbot, lambda: page.model.item('gain') is not None)


def test_params_edit_apply_reject_and_external(window, qtbot, dummy_system):
    from ros2_robot_dashboard.core.domain.params import ParamStatus
    window.navigate('params')
    page = window._pages['params']
    page.auto_apply.setChecked(False)
    _select_node(page, qtbot, '/test/dummy_params')
    # Hierarchy: controller.gains.kp is under controller > gains.
    assert page.model.item('controller.gains.kp') is not None
    assert page.model.item('fixed').info.descriptor.read_only

    page.on_param_edited(page.model.index_of('count'), 7)
    assert page.model.item('count').status == ParamStatus.MODIFIED
    page.apply_all()
    pump(qtbot, lambda: page.model.item('count').status == ParamStatus.OK)
    assert dummy_system.params.get_parameter('count').value == 7

    # Rejected by the node's validation callback.
    page.on_param_edited(page.model.index_of('gain'), 6.66)
    page.apply_all()
    pump(qtbot, lambda: page.model.item('gain').status == ParamStatus.REJECTED)

    # External change while the user has a local modification.
    page.discard_all()
    page.on_param_edited(page.model.index_of('name'), 'mine')
    dummy_system.set_param('name', 'theirs')
    pump(qtbot, lambda: page.model.item('name').status == ParamStatus.EXTERNAL_CHANGE)
    page.model.resolve_external('name', keep_mine=False)
    assert page.model.item('name').info.value == 'theirs'

    # Auto-apply with debounce.
    page.auto_apply.setChecked(True)
    page.on_param_edited(page.model.index_of('controller.gains.ki'), 0.25)
    pump(qtbot, lambda: dummy_system.params.get_parameter('controller.gains.ki').value == 0.25)


def test_params_preset_roundtrip(window, qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    from ros2_robot_dashboard.core.domain.params import ParamStatus
    window.navigate('params')
    page = window._pages['params']
    page.auto_apply.setChecked(False)
    _select_node(page, qtbot, '/test/dummy_params')
    path = str(tmp_path / 'preset.yaml')
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *a, **k: (path, ''))
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *a, **k: (path, ''))
    import ros2_robot_dashboard.adapters.qt.pages.params_page as pp
    monkeypatch.setattr(pp, 'info', lambda *a, **k: None)
    page.save_preset()
    assert os.path.isfile(path)
    import yaml
    data = yaml.safe_load(open(path))
    data['/test/dummy_params']['ros__parameters']['flag'] = False
    yaml.safe_dump(data, open(path, 'w'))
    page.load_preset()
    assert page.model.item('flag').status == ParamStatus.MODIFIED
    assert page.only_modified.isChecked()


def test_read_only_disables_write_widgets(window, qtbot, runtime):
    window.navigate('params')
    page = window._pages['params']
    runtime.safety.set_read_only(True)
    pump(qtbot, lambda: not page.apply_btn.isEnabled())
    assert window.ro_badge.isVisible() or window.ro_badge.isVisibleTo(window)
    runtime.safety.set_read_only(False)
    pump(qtbot, lambda: page.apply_btn.isEnabled())


def test_topics_echo_and_detail(window, qtbot):
    window.navigate('topics')
    page = window._pages['topics']
    pump(qtbot, lambda: any(t.name == '/joint_states' for t in page.model.rows))
    page.focus_item('topic', '/joint_states')
    pump(qtbot, lambda: page._selected == '/joint_states')
    assert page.ep_model.rows
    page.echo()
    pump(qtbot, lambda: 'position' in page.terminal.text())
    page.stop_echo()
    page._toggle_measure()
    pump(qtbot, lambda: page._rates.get('/joint_states', 0) > 10, 15000)
    page._toggle_measure()


def test_tf_tree_and_echo(window, qtbot):
    window.navigate('tf')
    page = window._pages['tf']
    pump(qtbot, lambda: 'tool0' in page.svc.snapshot().names())
    page.target.setEditText('world')
    page.source.setEditText('tool0')
    page._start_echo()
    pump(qtbot, lambda: 'translation' in page.out.toPlainText())
    page.fmt.setCurrentIndex(3)      # SE(3)
    assert page.out.toPlainText().count('\n') >= 4
    page._stop_echo()


def test_robot_joints_model_pose(window, qtbot):
    window.navigate('robot')
    page = window._pages['robot']
    pump(qtbot, lambda: len(page.model.rows) >= 2)
    pump(qtbot, lambda: page._model is not None and page._model.name == 'dummy_arm')
    page.parent_frame.setEditText('world')
    page.child_frame.setEditText('tool0')
    pump(qtbot, lambda: 'world <- tool0' in page.pose_out.toPlainText())


def test_controllers_list_and_switch(window, qtbot, monkeypatch):
    import ros2_robot_dashboard.adapters.qt.pages.controllers_page as cp
    from ros2_robot_dashboard.adapters.qt.pages.controllers_page import plan_text
    from PySide6.QtWidgets import QDialog
    monkeypatch.setattr(cp.SwitchDialog, 'exec', lambda self: QDialog.Accepted)
    window.navigate('controllers')
    page = window._pages['controllers']
    pump(qtbot, lambda: page.manager == '/fake/controller_manager')
    pump(qtbot, lambda: len(page.ctrl_model.rows) >= 2)
    inactive = [c.name for c in page.ctrl_model.rows if c.state == 'inactive']
    assert inactive
    target = inactive[0]
    plan = page.svc.plan_switch(page.manager, [target], [])
    holders = sorted({c.held_by for c in plan.conflicts})
    assert plan.conflicts and 'joint1/position' in plan_text(plan)   # conflict is shown
    page._do_switch([target], holders)
    pump(qtbot, lambda: any(c.name == target and c.state == 'active'
                            for c in page.ctrl_model.rows))
    # Switch back so the other tests see the initial state.
    page._do_switch(holders, [target])
    pump(qtbot, lambda: any(c.name == target and c.state == 'inactive'
                            for c in page.ctrl_model.rows))


def test_diagnostics_logs_lifecycle(window, qtbot):
    window.navigate('diagnostics')
    page = window._pages['diagnostics']
    pump(qtbot, lambda: len(page.logs.model.rows) > 0, 15000)
    pump(qtbot, lambda: page.diags.tree.topLevelItemCount() > 0)
    pump(qtbot, lambda: any(n.node == '/test/dummy_lifecycle'
                            for n in page.lifecycle.model.rows), 15000)


def test_graph_layout_and_export(window, qtbot, tmp_path):
    window.navigate('graph')
    page = window._pages['graph']
    page.hide_dead.setChecked(False)
    page.hide_isolated.setChecked(False)
    pump(qtbot, lambda: 'n:/test/dummy_topics' in page._shapes, 15000)
    for ext in ('png', 'svg', 'dot'):
        out = tmp_path / f'graph.{ext}'
        page.export(str(out))
        assert out.stat().st_size > 0
    page.focus_item('node', '/test/dummy_topics')
    assert page._shapes['n:/test/dummy_topics'].isSelected()


def test_services_and_actions_page(window, qtbot, dummy_system):
    if dummy_system.interfaces is None:
        pytest.skip('example_interfaces not installed')
    window.navigate('services')
    page = window._pages['services']
    pump(qtbot, lambda: any(s.name == '/test/add_two_ints' for s in page.services.model.rows))
    page.focus_item('service', '/test/add_two_ints')
    pump(qtbot, lambda: page.services.current is not None)
    page.services.form.yaml_mode.setChecked(True)
    page.services.form.yaml_edit.setPlainText('a: 20\nb: 22\n')
    page.services._call()
    pump(qtbot, lambda: 'sum: 42' in page.services.result.toPlainText())

    pump(qtbot, lambda: any(a.name == '/test/fibonacci' for a in page.actions.model.rows))
    page.focus_item('action', '/test/fibonacci')
    pump(qtbot, lambda: page.actions.current is not None)
    page.actions.form.yaml_mode.setChecked(True)
    page.actions.form.yaml_edit.setPlainText('order: 4\n')
    page.actions._call()
    pump(qtbot, lambda: '# result (succeeded)' in page.actions.result.toPlainText(), 15000)


def test_utilities_compare_and_plot(window, qtbot):
    window.navigate('utilities')
    page = window._pages['utilities']
    page.compare.refresh_nodes()
    page.compare.left.setCurrentText('/test/dummy_params')
    page.compare.right.setCurrentText('/test/dummy_topics')
    page.compare._compare_nodes()
    pump(qtbot, lambda: page.compare.model.rows)
    page.plot.topic.setEditText('/joint_states')
    page.plot.field.setEditText('position[0]')
    page.plot._add()
    assert page.plot.sources
    page.plot.stop_all()


def test_search_dialog(window, qtbot):
    from ros2_robot_dashboard.adapters.qt.main_window import SearchDialog
    dlg = SearchDialog(window)
    dlg.edit.setText('joint_states')
    dlg._search()
    assert dlg.results.count() >= 1
    dlg._open_first()
    assert window.current_page_id() == 'topics'


def test_main_cli_parses_ros_args():
    from ros2_robot_dashboard.cli import parse
    a = parse(['--read-only', '--page', 'tf', '--ros-args', '-r', '__node:=x'])
    assert a.read_only and a.page == 'tf'
    assert QApplication.instance() is not None


def test_launch_page_start_stop_demo_profile(window, qtbot, runtime, tmp_path):
    """Start/stop a short process through the Launch page (no ROS launch needed)."""
    from ros2_robot_dashboard.core.domain.process import ProcessProfile
    prof = ProcessProfile('sleeper', 'Sleeper', 'sleep 30', 'test process')
    runtime.launch.save_profile(prof, str(tmp_path / 'sleeper.yaml'))
    runtime.config.set('launch.profile_dirs', [str(tmp_path)])
    window.navigate('launch')
    page = window._pages['launch']
    runtime.launch.reload()
    pump(qtbot, lambda: any(page.list.item(i).data(0x0100) == 'sleeper'
                            for i in range(page.list.count())))
    page.select_profile('sleeper')
    page.select_profile('sleeper')            # selecting twice must keep it current
    assert page.current == 'sleeper'
    page._start()
    pump(qtbot, lambda: runtime.launch.status('sleeper').state.value == 'running')
    page._stop()
    pump(qtbot, lambda: not runtime.launch.status('sleeper').state.is_active, 20000)
    assert not runtime.launch.status('sleeper').leftover_pids
