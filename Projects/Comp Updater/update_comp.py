import os
from optparse import OptionParser

import nuke
import sgtk

import shotgun_utils
from shotgun_utils import auth

import compUpdater
import compBuilderKarma
from nuke_pipeline import artist_api


def main():
    usage = 'update_comp.py usage:\n\n'
    usage += '-s --shot shot_code eg: vfx01_120 (required)\n'
    usage += '--preview print the farm job instead of sending it\n'
    usage += '--no-sg-upload render exrs only, no png, mov or Flow version\n'
    usage += 'example: update_comp.py -s vfx01_120 --preview\n'

    p = OptionParser(usage=usage)
    p.add_option("-s", "--shot", dest='shot', action='store')
    p.add_option("--preview", dest='preview', action='store_true', default=False)
    p.add_option("--no-sg-upload", dest='no_sg_upload', action='store_true', default=False)

    opts, args = p.parse_args()

    if not opts.shot:
        p.error("Missing required argument (-s).")

    task = find_comp_task(opts.shot)

    bootstrap_engine(task)

    submitter = os.environ.get("CREWNAME")
    if not submitter:
        raise RuntimeError("CREWNAME is not set, can't tell whose comp workfile to update")
    print("Submitter:", submitter)

    latest_path = compUpdater.find_latest_workfile(opts.shot, submitter)[0]
    print("Latest workfile:", latest_path)

    next_path = compUpdater.open_next_version(latest_path)
    print("Next version:", next_path)

    loader = compBuilderKarma.Loader()
    if not loader.latest:
        raise RuntimeError("No lighting renders found for shot %s" % opts.shot)
    print("Latest lighting version:", loader.latest)

    before = get_read_paths()
    loader.updateLighting(loader.latest)
    print_read_updates(before, get_read_paths())

    print_camera_update(compUpdater.update_camera(opts.shot))

    nuke.scriptSave(nuke.root().name())
    print("Saved:", nuke.root().name())

    write_node, render_path, frames = compUpdater.get_render_settings()
    print("Render path:", render_path)
    print("Frames:", frames)

    write_node_name = write_node.name()
    launch_review_farm_job(write_node, frames, sg_upload=not opts.no_sg_upload, preview=opts.preview)

    compUpdater.tag_comp_write_node(nuke.toNode(write_node_name))

    nuke.scriptSave(nuke.root().name())


def find_comp_task(shot):
    sg = shotgun_utils.get_tk().shotgun
    project_id = shotgun_utils.get_project_id()

    task = sg.find_one(
        "Task",
        [
            ["entity.Shot.code", "is", shot],
            ["step.Step.code", "is", "comp"],
            ["content", "is", "comp"],
            ["project.Project.id", "is", project_id],
        ],
        ["id", "content", "entity"]
    )

    if not task:
        raise RuntimeError("No comp task found for shot %s" % shot)

    return task


def bootstrap_engine(task):
    engine = sgtk.platform.current_engine()
    if engine:
        print("Engine already running: %s" % engine)
        return engine

    return auth.bootstrap_engine_from_entity(task, "tk-nuke")


def launch_review_farm_job(write_node, frames, sg_upload=True, preview=False):
    colorspace = write_node.knob("colorspace").value()
    if colorspace in ("linear", "default (linear)", "default (scene_linear)"):
        write_node.knob("colorspace").setValue("scene_linear")
        print("Colorspace: %s -> scene_linear" % colorspace)

    return artist_api.farm_render(write_node.name(), frames=frames, batch_size=10, sg_upload=sg_upload, preview=preview)


def get_read_paths():
    return {node.name(): node['file'].value() for node in nuke.allNodes('Read')}


def print_read_updates(before, after):
    print("Read nodes:")
    for name, old_path in before.items():
        new_path = after.get(name)
        if new_path != old_path:
            print("  updated %s: %s -> %s" % (name, old_path, new_path))
        else:
            print("  unchanged %s: %s" % (name, old_path))


def print_camera_update(update):
    if not update:
        print("Camera node: none found for this shot")
        return

    name, old_path, new_path = update
    if new_path != old_path:
        print("Camera node: updated %s: %s -> %s" % (name, old_path, new_path))
    else:
        print("Camera node: unchanged %s: %s" % (name, old_path))


if __name__ == "__main__":
    main()