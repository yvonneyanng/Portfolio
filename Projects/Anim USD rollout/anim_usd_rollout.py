import os
import sys
import argparse

import maya.standalone
maya.standalone.initialize(name="python")

import maya.cmds as cmds
import sgtk
import shotgun_utils
from maya_pipeline.ala_tools import ala_UsdRollout

# rez-env s126 maya_pipeline maya -- mayapy anim_usd_rollout.py -s obs01_050 -a charMonster01


def find_animation_task(shot):
    sg = shotgun_utils.get_tk().shotgun
    project_id = shotgun_utils.get_project_id()

    task = sg.find_one(
        "Task",
        [
            ["entity.Shot.code", "is", shot],
            ["step.Step.code", "is", "animation"],
            ["content", "is", "animation"],
            ["project.Project.id", "is", project_id],
        ],
        ["id", "content", "entity"]
    )

    if not task:
        raise RuntimeError("No animation task found for shot %s" % shot)

    return task


def bootstrap_engine(task):
    engine = sgtk.platform.current_engine()
    if engine:
        print("Engine already running: %s" % engine)
        return engine

    # tk-maya sets the Maya project to the running user's own work folder, which doesn't exist when they never worked on the shot, so don't let that stop the bootstrap
    original_workspace = cmds.workspace

    def skip_project_errors(*args, **kwargs):
        try:
            return original_workspace(*args, **kwargs)
        except RuntimeError as e:
            if "Cannot open project" not in str(e):
                raise
            print("Skipped setting Maya project: %s" % e)

    cmds.workspace = skip_project_errors
    try:
        return shotgun_utils.auth.bootstrap_engine_from_entity(task, "tk-maya")
    finally:
        cmds.workspace = original_workspace


def print_references():
    refs = cmds.file(query=True, reference=True) or []
    print("References: %d" % len(refs))
    for ref in refs:
        area = "WIP" if "/wip/" in ref else "published"
        print("  [%s] %s" % (area, ref))


def print_publish_tasks(manager):
    print("Publish items:")
    for item in manager.tree:
        active = [task.plugin.name for task in item.tasks if task.active]
        print("  %s (%s): %s" % (item.name, item.type_spec, ", ".join(active) or "-"))


def main():
    parser = argparse.ArgumentParser(description="Update rigs and publish caches for one shot, headless.")
    parser.add_argument("-s", "--shot", help="Shot code, e.g. obs01_050")
    parser.add_argument("-a", "--asset", required=True, help="Rig asset being rolled out, e.g. charMonster01")
    parser.add_argument("--notify-log-dir", help="Instead of rolling out a shot, post a Flow note summarising the shot logs in this folder")
    parser.add_argument("--notify-shots", nargs="+", default=[], help="Shots to include in the summary note")
    parser.add_argument("--notify-user", type=int, help="HumanUser id to address the summary note to")
    args = parser.parse_args()

    if args.notify_log_dir:
        ala_UsdRollout.post_summary_note(args.asset, args.notify_shots, args.notify_log_dir, args.notify_user)
        return

    if not args.shot:
        parser.error("--shot is required unless --notify-log-dir is given")

    print("Shot: %s" % args.shot)
    print("Asset: %s" % args.asset)
    print("Project: %s" % os.environ.get("SHOTGUN_PROJECT"))

    task = find_animation_task(args.shot)
    print("Task: %s" % task)

    engine = bootstrap_engine(task)
    print("Engine: %s" % engine)
    print("Context: %s" % engine.context)
    print("Step: %s" % (engine.context.step or {}).get("name"))

    scene = ala_UsdRollout.open_next_version(args.shot)
    print("Opened: %s" % cmds.file(query=True, sceneName=True))

    # Work in the artist's folder the file was opened from
    cmds.workspace(os.path.dirname(scene), openWorkspace=True)
    print("Maya project: %s" % cmds.workspace(query=True, rootDirectory=True))

    print_references()

    ala_UsdRollout.update_rig(args.asset)

    for plugin in ("mayaUsdPlugin", "AbcExport"):
        cmds.loadPlugin(plugin, quiet=True)

    manager = ala_UsdRollout.validate_animcaches()
    print("Saved: %s" % cmds.file(query=True, sceneName=True))
    print_publish_tasks(manager)
    print("Validation passed")

    manager.publish()
    manager.finalize()
    print("Published")


if __name__ == "__main__":
    try:
        main()
        print("[RESULT] OK", flush=True)
    except Exception as e:
        import traceback
        traceback.print_exc()
        reason = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        print("[RESULT] FAILED: %s" % reason, flush=True)
        sys.exit(1)
    finally:
        maya.standalone.uninitialize()