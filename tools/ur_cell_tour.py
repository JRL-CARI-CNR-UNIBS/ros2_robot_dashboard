#!/usr/bin/env python3
"""End-to-end tour with the UR mock cell; optionally saves documentation screenshots.

The cell is started and stopped *by the dashboard* (Launch page), so this also tests the
process management. Run it inside a sourced workspace with ``ur_robot_driver``::

    ROS_DOMAIN_ID=77 python3 tools/ur_cell_tour.py --shots docs/images

Exit code 0 when every check passed. Uses an isolated ROS_HOME and runs offscreen
unless ``--show`` is given.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main() -> int:
    import faulthandler
    import signal as _signal
    faulthandler.register(_signal.SIGUSR1, all_threads=True)   # kill -USR1 <pid>: stacks
    ap = argparse.ArgumentParser()
    ap.add_argument('--shots', help='directory for screenshots')
    ap.add_argument('--show', action='store_true', help='use the real display')
    ap.add_argument('--distro', default=os.environ.get('ROS_DISTRO', ''))
    ap.add_argument('--timeout', type=float, default=60.0)
    args = ap.parse_args()
    if not args.show:
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    os.environ.setdefault('ROS_HOME', tempfile.mkdtemp(prefix='dash_tour_'))
    os.environ.setdefault('XDG_STATE_HOME', os.path.join(os.environ['ROS_HOME'], 'state'))

    from ros2_robot_dashboard.adapters.qt.env_fix import prepare_fontconfig
    restore = prepare_fontconfig()
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication(['ur_cell_tour'])
    restore()
    from ros2_robot_dashboard.adapters.qt import theme
    from ros2_robot_dashboard.adapters.qt.main_window import MainWindow
    from ros2_robot_dashboard.core.config import Config
    from ros2_robot_dashboard.core.runtime import CoreRuntime

    QMessageBox.exec = lambda self: QMessageBox.Cancel        # never block
    theme.apply_theme(app, 'light')
    cfg = Config.load(overrides={'confirm_actions': False})
    rt = CoreRuntime(cfg).start()
    # Never leave the cell running, even on errors or SIGTERM.
    import atexit
    import signal
    atexit.register(lambda: rt.launch.running() and rt.launch.stop_all())
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(2))
    settings = QSettings(os.path.join(os.environ['ROS_HOME'], 'tour.ini'), QSettings.IniFormat)
    w = MainWindow(rt, settings, initial_page='launch')
    w.resize(1440, 880)
    w.show()
    failures: list[str] = []
    distro = args.distro or rt.compat.distro()

    def spin(seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            time.sleep(0.01)

    def wait(pred, timeout: float, what: str) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            app.processEvents()
            try:
                if pred():
                    return True
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.05)
        failures.append(f'timeout: {what}')
        print(f'FAIL {what}', flush=True)
        return False

    def shot(name: str) -> None:
        spin(0.6)
        if args.shots:
            os.makedirs(args.shots, exist_ok=True)
            path = os.path.join(args.shots, f'{name}.png')
            w.grab().save(path)
            print(f'saved {path}', flush=True)

    def ok(msg: str) -> None:
        print(f'ok   {msg}', flush=True)

    # ------------------------------------------------------------- launch the cell
    launch = w.page('launch')
    w.navigate('launch')
    launch.select_profile('ur_mock_cell')
    spin(0.5)
    values = launch.form.values()
    values['ur_type'] = 'ur5e'
    values['launch_rviz'] = False
    # Humble uses use_fake_hardware, newer distros use_mock_hardware.
    values['use_mock_hardware'] = distro != 'humble'
    values['use_fake_hardware'] = distro == 'humble'
    launch.form.set_values(values)
    launch._update_preview()
    launch._start()
    if not wait(lambda: rt.launch.status('ur_mock_cell').state.value == 'running', 15,
                'launch started'):
        return finish(rt, w, failures)
    ok('cell started from the Launch page')
    if not wait(lambda: rt.controllers.managers(), args.timeout,
                'controller manager discovered'):
        return finish(rt, w, failures)
    manager = rt.controllers.managers()[0]
    rt.controllers.watch(manager, 'tour')
    wait(lambda: rt.controllers.last_state(manager) and sum(
        1 for c in rt.controllers.last_state(manager).controllers if c.state == 'active') >= 2,
        args.timeout, 'controllers active')
    spin(2)
    launch.terminal.set_filter('')
    shot('launch')

    # ------------------------------------------------------------- parameters
    params = w.page('params')
    w.navigate('params')
    state = rt.controllers.last_state(manager)
    active = [c.name for c in state.controllers if c.state == 'active'] if state else []
    jtc = next((c for c in active if 'joint_trajectory' in c), active[0] if active else '')
    node = '/' + jtc if jtc and not jtc.startswith('/') else jtc
    wait(lambda: params.node_model.row_of(node) >= 0, 20, f'node {node} listed')
    params.focus_item('node', node)
    if wait(lambda: params.model.items(), 20, 'parameters loaded'):
        ok(f'{len(params.model.items())} parameters of {node}')
        params.tree.expandAll()
    shot('params')

    # ------------------------------------------------------------- topics
    topics = w.page('topics')
    w.navigate('topics')
    wait(lambda: any(t.name == '/joint_states' for t in topics.model.rows), 10, 'topics')
    topics.focus_item('topic', '/joint_states')
    topics.filter.setText('')
    topics.echo()
    topics._toggle_measure()
    wait(lambda: 'position' in topics.terminal.text(), 10, 'echo /joint_states')
    wait(lambda: topics._rates.get('/joint_states', 0) > 10, 10, 'rate of /joint_states')
    ok(f"/joint_states at {topics._rates.get('/joint_states', 0):.0f} Hz")
    shot('topics')
    topics.stop_echo()
    topics._toggle_measure()

    # ------------------------------------------------------------- services
    srv = w.page('services')
    w.navigate('services')
    srv.focus_item('service', f'{manager}/list_controllers')
    srv.services.filter.setText('controller_manager/list')
    spin(0.5)
    srv.services._call()
    wait(lambda: 'controller' in srv.services.result.toPlainText(), 10,
         'list_controllers call from the Services page')
    shot('services')

    # ------------------------------------------------------------- graph
    graph = w.page('graph')
    w.navigate('graph')
    if not wait(lambda: len(graph._shapes) >= 3, 20, 'graph drawn'):
        print(f'info graph: {len(graph._shapes)} shapes, status "{graph.status.text()}"',
              flush=True)
    spin(1)
    graph._fit()
    shot('graph')

    # ------------------------------------------------------------- tf
    tf = w.page('tf')
    w.navigate('tf')
    wait(lambda: 'tool0' in rt.tf.snapshot().names(), 15, 'TF tool0')
    tf.target.setEditText('base_link')
    tf.source.setEditText('tool0')
    tf.fmt.setCurrentIndex(2)
    tf._start_echo()
    wait(lambda: 'translation' in tf.out.toPlainText(), 10, 'tf echo base_link <- tool0')
    ok('TF echo base_link <- tool0')
    shot('tf')
    tf._stop_echo()

    # ------------------------------------------------------------- robot
    robot = w.page('robot')
    w.navigate('robot')
    wait(lambda: len(robot.model.rows) >= 6, 15, 'six joints')
    wait(lambda: robot._model is not None, 15, 'robot description')
    robot.parent_frame.setEditText('base_link')
    robot.child_frame.setEditText('tool0')
    wait(lambda: 'base_link <- tool0' in robot.pose_out.toPlainText(), 10, 'robot pose')
    ok(f'{len(robot.model.rows)} joints, model {robot._model.name if robot._model else "?"}')
    shot('robot')

    # ------------------------------------------------------------- controllers
    ctrl = w.page('controllers')
    w.navigate('controllers')
    wait(lambda: ctrl.ctrl_model.rows, 10, 'controllers listed')
    inactive = [c.name for c in ctrl.ctrl_model.rows if c.state == 'inactive']
    preview = next((c for c in inactive if c == 'forward_position_controller'),
                   inactive[0] if inactive else '')
    if preview:
        # Select an inactive controller to show the switch preview (with conflicts).
        for r in range(ctrl.ctrl_proxy.rowCount()):
            idx = ctrl.ctrl_proxy.index(r, 0)
            if idx.data() == preview:
                ctrl.ctrl_table.selectRow(r)
                break
    shot('controllers')
    from PySide6.QtWidgets import QDialog
    from ros2_robot_dashboard.adapters.qt.pages import controllers_page as cp
    cp.SwitchDialog.exec = lambda self: QDialog.Accepted
    fwd = next((c for c in inactive if 'forward_position' in c or 'position' in c), '')
    if fwd and jtc:
        ctrl._do_switch([fwd], [jtc])
        if wait(lambda: any(c.name == fwd and c.state == 'active'
                            for c in ctrl.ctrl_model.rows), 15, f'switch to {fwd}'):
            ok(f'switched {jtc} -> {fwd}')
        ctrl._do_switch([jtc], [fwd])
        wait(lambda: any(c.name == jtc and c.state == 'active' for c in ctrl.ctrl_model.rows),
             15, f'switch back to {jtc}')
    ctrl.tabs.setCurrentIndex(1)
    shot('controllers_interfaces')
    ctrl.tabs.setCurrentIndex(0)

    # ------------------------------------------------------------- diagnostics
    diag = w.page('diagnostics')
    w.navigate('diagnostics')
    wait(lambda: diag.logs.model.rows, 10, 'logs')
    shot('logs')

    # ------------------------------------------------------------- bags
    bags = w.page('bags')
    w.navigate('bags')
    bags.record.filter.setText('joint')
    spin(0.5)
    bags.record.topics.selectAll()
    bags.record._preview()
    shot('bags')

    # ------------------------------------------------------------- health
    health = w.page('health')
    w.navigate('health')
    idx = health.profile.findData('ros2_control_cell')
    if idx >= 0:
        health.profile.setCurrentIndex(idx)
        health._profile_changed(idx)
    wait(lambda: health.host_model.rows, 10, 'host metrics')
    spin(3)
    shot('health')

    # ------------------------------------------------------------- utilities + action
    util = w.page('utilities')
    w.navigate('utilities')
    util.plot.refresh_topics()
    util.plot.topic.setEditText('/joint_states')
    for field in ('position[0]', 'position[1]', 'velocity[0]'):
        util.plot.field.setEditText(field)
        util.plot._add()
    spin(1)
    # Move the (mock) robot with a FollowJointTrajectory goal from the Actions page.
    action = f'/{jtc}/follow_joint_trajectory'
    w.navigate('services')
    srv.focus_item('action', action)
    wait(lambda: srv.actions.current is not None and srv.actions.current.name == action, 10,
         f'action {action} listed')
    joints = sorted(j.state.name for j in robot.model.rows)
    order = ['shoulder_pan_joint', 'shoulder_lift_joint', 'elbow_joint', 'wrist_1_joint',
             'wrist_2_joint', 'wrist_3_joint']
    names = [j for j in order if j in joints] or joints
    start = {r.state.name: r.state.position or 0.0 for r in robot.model.rows}
    p0 = [start.get(n, 0.0) for n in names]
    p1 = list(p0)
    p1[0] += 0.8
    if len(p1) > 2:
        p1[1] += 0.4
        p1[2] += 0.6
    srv.actions.form.yaml_mode.setChecked(True)
    srv.actions.form.yaml_edit.setPlainText(
        'trajectory:\n'
        f'  joint_names: [{", ".join(names)}]\n'
        '  points:\n'
        f'  - positions: [{", ".join(f"{v:.3f}" for v in p1)}]\n'
        '    time_from_start: {sec: 2, nanosec: 0}\n'
        f'  - positions: [{", ".join(f"{v:.3f}" for v in p0)}]\n'
        '    time_from_start: {sec: 4, nanosec: 0}\n')
    srv.actions._call()
    wait(lambda: '# feedback' in srv.actions.result.toPlainText(), 10, 'action feedback')
    spin(1.5)
    shot('actions')
    if wait(lambda: '# result (succeeded)' in srv.actions.result.toPlainText(), 15,
            'trajectory succeeded'):
        ok('FollowJointTrajectory goal succeeded')
    w.navigate('utilities')
    spin(1)
    shot('utilities')
    util.plot.stop_all()

    # ------------------------------------------------------------- dark theme
    w.dark_action.setChecked(True)
    w.navigate('controllers')
    shot('dark_theme')
    w.dark_action.setChecked(False)

    # ------------------------------------------------------------- stop the cell
    w.navigate('launch')
    launch.select_profile('ur_mock_cell')
    t_stop = time.time()
    launch._stop()
    wait(lambda: not rt.launch.status('ur_mock_cell').state.is_active, 60, 'cell stopped')
    print(f'info stop took {time.time() - t_stop:.1f} s', flush=True)
    st = rt.launch.status('ur_mock_cell')
    if st.leftover_pids:
        failures.append(f'leftover processes: {st.leftover_pids} {st.message}')
        rt.launch.kill_leftovers('ur_mock_cell')
    else:
        ok('cell stopped, no leftover processes')
    return finish(rt, w, failures)


def finish(rt, w, failures) -> int:
    try:
        if rt.launch.running():
            rt.launch.stop_all()
    finally:
        for p in w._pages.values():
            p.shutdown()
        w.bridge.close()
        rt.stop()
    if failures:
        print('FAILURES:\n  ' + '\n  '.join(failures))
        return 1
    print('ALL CHECKS PASSED')
    return 0


if __name__ == '__main__':
    sys.exit(main())
