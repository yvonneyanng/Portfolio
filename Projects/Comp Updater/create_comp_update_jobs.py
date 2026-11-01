import os
import subprocess

import shotgun_utils
import tractor.api.author


def get_update_cmd(shot, extra_args=""):
    proj_code = shotgun_utils.get_project_code()

    script = f'$NUKE_BIN -t -- $REZ_NUKE_PIPELINE_ROOT/bin/update_comp.py -s {shot} {extra_args}'.strip()

    return [
        '/opt/rez/bin/rez/rez-env',
        'nuke_pipeline',
        'nuke',
        proj_code,
        '--',
        'bash',
        '-c',
        script,
    ]


def make_update_job(shot, extra_args=""):
    proj_code = shotgun_utils.get_project_code()

    job_title = f'{shot}_comp_update_begin'

    job = tractor.api.author.Job()
    job.title = job_title
    job.envkey = ['rez-pkgs=nuke_pipeline nuke {0}'.format(proj_code)]
    job.atleast = 1
    job.service = 'bash'
    job.tier = 'default'
    job.atmost = job.atleast

    job.newTask(title=job_title, argv=get_update_cmd(shot, extra_args))

    return job

'''
@yvonne.yang
send_update_jobs and run_update_jobs_locally run the same update_comp.py command per shot.
send_update_jobs sends each shot as a Tractor job, but it only works with a released nuke_pipeline.
run_update_jobs_locally runs them in parallel on this machine with the local build, for testing before a release.
Change the call in compUpdater.py to run_update_jobs_locally for testing, and change it back to send_update_jobs when ready to release.
'''

def send_update_jobs(shots, preview=False, extra_args=""):
    job_ids = {}
    for shot in shots:
        job = make_update_job(shot, extra_args)
        if preview:
            print(job.asTcl())
            continue
        job_ids[shot] = job.spool()
    return job_ids


def run_update_jobs_locally(shots, extra_args=""):
    log_dir = os.path.join(os.path.expanduser("~"), "comp_updater_local")
    os.makedirs(log_dir, exist_ok=True)

    env = {key: value for key, value in os.environ.items() if not key.startswith(("TANK_", "SGTK_"))}

    processes = {}
    for shot in shots:
        log_path = os.path.join(log_dir, f"{shot}.log")
        log_file = open(log_path, "w")
        processes[shot] = subprocess.Popen(get_update_cmd(shot, extra_args), stdout=log_file, stderr=subprocess.STDOUT, env=env)
        print("Started %s (pid %s), log: %s" % (shot, processes[shot].pid, log_path))
    return processes