"""
@yvonne.yang
This script builds a comp template for VFX/CG shots, many stuffs are hardcoded cause passes have their own custom setup, even some nodes' knob values are 
set in advance based on project needs, so some values might not be valid for future projects. 
You'll need to check with lighting and comp team to get the correct values for your project, as the pass order matters and some passes have custom setup that needs to be built in a certain way.
Comp team would usually build the template with necessary passes already, and that's when you'll start updating this script, big changes small changes who knows :)

How a template is born:
1. Build the main lane passes in order, each pass is built in a column with a read node, a distortion block, empty adjust backdrop.
2. Build the beauty and plate in the middle.
3. Build the secondary lane passes in order, each pass is built in a column with a read node and a distortion block, and any custom setup.
4. Wire the secondary lane passes.
5. Chain the main lane passes together.
6. Merge the main lane result and the secondary lane result.
7. Build the lens treatment block and output block at the bottom.

-----------------------------------------------
Here are the main few things to look at for project-specific changes:

1. PASS_CONFIG: defines the pass order in the comp (dict order = comp order) and each pass's settings:
- label (comp label)
- lane (main or secondary)
- raw (pulls in _raw render data if True), 
and for secondary passes operation/mix/indicators are for how it merges onto the plate. 
PASS_ORDER is just a (name, label) list derived from it don't edit it directly. Passes found on disk that aren't in PASS_CONFIG get appended to the end of the
secondary lane automatically.

2. create{PassName}Pass: any custom setup will be translated into a function named like this, and the function will be called via the passFns dict built by getPassFns.

3. getPassFns: add any custom pass functions to the passFns dict here, with the key being the pass name, and the value being a lambda function that calls the custom function with the required arguments.

-----------------------------------------------
The guide to modify the comp template:

1. Check with comp team for the pass order and per-pass settings, and define them in PASS_CONFIG, there might be changes later on but the first thing you need to do is update this dictionary.

2. If any pass has a custom setup, create a new function named like create{PassName}Pass, and put the custom nodes in that function.
- The read node and distortion block are already built before your function is called, so you don't need to build those again.
- Every pass gets an ADJUST backdrop, either put custom setup into that same spot if any, or call createAdjustPlaceholder to leave it as an empty placeholder.
- Return your pass's own tail node (like createShadowPass returning node_Invert_1), the caller wires it into the chain with mergePass afterward, you don't call that yourself.

3. If custom functions are created, add them to the passFns dict in getPassFns, with the key being the pass name,
and the value being a lambda function that calls the custom function with the required arguments.

To translate nodes to python code, use the following script built by David Emeny:
https://drive.google.com/file/d/1lKjJFkjJdPaNeRWPIwF4e6Hoogg5edxk/view

----------------------------------------------
Some notes:

Bring In New Passes button only handles main-lane inserts, as inserting new secondary pass is too much of adjustment to do automatically, plus comp team would usually bring in a fresh template, which 
will contain the new secondary pass, and move their existing work over to the new template. More of a nice to have feature so it's not fully automated, but it does help with main-lane pass updates.

Good luck :)

"""

import os
import re
from pathlib import Path

import nuke
import nuke.rotopaint as rp
import nukescripts
from nukescripts import panels

if nuke.NUKE_VERSION_MAJOR < 11:
    from PySide import QtCore, QtWidgets
elif nuke.NUKE_VERSION_MAJOR < 16:
    from PySide2 import QtCore, QtWidgets
else:
    from PySide6 import QtCore, QtWidgets

import sgtk
import shotgun_utils
from turret import resolver

projectCode = str(shotgun_utils.get_project_code())

# Fixed order requested by comp team.
# label: the label to display in the comp for this pass
# lane: which lane this pass belongs to, main or secondary
# raw: whether this pass needs to pull in the _raw render data
# puzzleMattes: whether this pass needs to pull in puzzle mattes
# operation/mix/indicators: for secondary passes, how it merges onto the plate 
PASS_CONFIG = {
    'char':              {'label': 'CHAR',   'lane': 'main', 'raw': True, 'puzzleMattes': True},
    'char01':            {'label': 'CHAR 01',   'lane': 'main', 'raw': True, 'puzzleMattes': True},
    'char02':            {'label': 'CHAR 02',   'lane': 'main', 'raw': True, 'puzzleMattes': True},
    'char03':            {'label': 'CHAR 03',   'lane': 'main', 'raw': True, 'puzzleMattes': True},
    'char04':            {'label': 'CHAR 04',   'lane': 'main', 'raw': True, 'puzzleMattes': True},
    'monster':           {'label': 'MONSTER',   'lane': 'main', 'raw': True, 'puzzleMattes': True},
    'prop':              {'label': 'PROPS',  'lane': 'main', 'raw': True, 'puzzleMattes': True},
    'env':               {'label': 'ENV',    'lane': 'main', 'raw': True, 'puzzleMattes': True},
    'fx':                {'label': 'FX',     'lane': 'main', 'raw': True},
    'fog':               {'label': 'FOG',    'lane': 'main', 'raw': True},
    'volume':            {'label': 'VOLUME', 'lane': 'main', 'raw': True},
    'envshd':            {'label': 'SHADOW', 'lane': 'secondary', 'operation': 'multiply'},
    'ambient_occlusion': {'label': 'AO',     'lane': 'secondary', 'operation': 'multiply', 'mix': 0.655, 'indicators': 16.0},
    'env_refl_occlu':    {'label': 'REFL O', 'lane': 'secondary', 'operation': None},
    'env_reflection':    {'label': 'REFL',   'lane': 'secondary', 'operation': 'screen', 'mix': 0.5, 'indicators': 16.0},
}
PASS_ORDER = [(passName, cfg['label']) for passName, cfg in PASS_CONFIG.items()]

EXCLUDED_PASS_NAMES = {'macbethprobe'}

SECONDARY_TAP_SPACING = 400

PROJECT_FORMAT = next((f for f in nuke.formats() if f.name() == '4K_DCP'), None)
if PROJECT_FORMAT is None:
    nuke.warning('4K_DCP format not found, falling back to the current root format.')
    PROJECT_FORMAT = nuke.root().format()

_SEQUENCE_FRAME_RE = re.compile(r'\.\d+\.[^./]+$')


def _registerProjectFormat(detectedFormat) -> "nuke.Format":
    width, height = detectedFormat.width(), detectedFormat.height()
    pixelAspect = detectedFormat.pixelAspect()
    existing = next(
        (f for f in nuke.formats() if f.width() == width and f.height() == height and f.pixelAspect() == pixelAspect),
        None
    )
    if existing:
        return existing

    name = 'project_{}x{}'.format(int(width), int(height))
    nuke.addFormat('{} {} {} {}'.format(int(width), int(height), pixelAspect, name))
    return next((f for f in nuke.formats() if f.name() == name), detectedFormat)


class Loader():
    def __init__(self):
        self.shotPath = ""
        self.publishes = ""
        self.versions = ""
        self.latest = ""
        self.assetName = ""
        self.startFrame = ""
        self.endFrame = ""
        self.tk = ""
        self.isContext = False
        if sgtk.platform.current_engine().context.entity_locations:
            self.shotPath = sgtk.platform.current_engine().context.entity_locations[0]

            publishes = Path(self.shotPath) / 'departments/light/lighting/renders'
            if not publishes.is_dir() or not any(publishes.iterdir()):
                publishes = Path(self.shotPath) / 'departments/light/lighting/review'
            if not publishes.is_dir():
                return

            versions = sorted((p.name for p in publishes.iterdir() if p.is_dir()), reverse=True)
            if not versions:
                return

            self.publishes = str(publishes)
            self.versions = versions
            self.latest = versions[0]
            self.assetName = sgtk.platform.current_engine().context.entity['name']
            self.tk = shotgun_utils.get_tk()
            shotId = sgtk.platform.current_engine().context.entity['id']
            self.startFrame = str(self.get_sg_frame_range(shotId)[0])
            self.endFrame = str(self.get_sg_frame_range(shotId)[1])
            self.isContext = True

            if self.startFrame.isdigit() and self.endFrame.isdigit():
                nuke.root()['first_frame'].setValue(int(self.startFrame))
                nuke.root()['last_frame'].setValue(int(self.endFrame))
                nuke.root()['lock_range'].setValue(True)
        else:
            return

    def get_sg_frame_range(self, shotId):
        head_in = self.tk.shotgun.find_one('Shot', [['id', 'is', shotId]], ['sg_head_in'])['sg_head_in']
        tail_out = self.tk.shotgun.find_one('Shot', [['id', 'is', shotId]], ['sg_tail_out'])['sg_tail_out']
        return head_in, tail_out

    def refreshVersions(self) -> None:
        if not self.isContext:
            return
        versions = sorted((p.name for p in Path(self.publishes).iterdir() if p.is_dir()), reverse=True)
        if versions:
            self.versions = versions
            self.latest = versions[0]
    
    # ---------------------------------------------------------------------------- #
    #                              Read node creation                              #
    # ---------------------------------------------------------------------------- #
    def get_padding(self, seqNumbers) -> str:
        '''
        Get correct number of padding for image sequence
        '''
        return '#' * len(seqNumbers)

    def listSequenceFiles(self, directory) -> list:
        '''
        List all files in a directory that match the image sequence pattern.
        '''
        entries = sorted(os.listdir(directory))
        return [
            f for f in entries
            if os.path.isfile(os.path.join(directory, f)) and _SEQUENCE_FRAME_RE.search(f)
        ]

    def createSeqPath(self, imageSequenceFolder, files=None) -> tuple[str, str]:
        '''
        Create a nuke image sequence path with proper padding count
        '''
        if files is None:
            files = self.listSequenceFiles(imageSequenceFolder)

        if files:
            seqNumbers = files[0].split('.')[-2]
            denoised = files[0].split('.')[0] + '.denoised.' + seqNumbers + '.exr'
            padding = self.get_padding(seqNumbers)

            if os.path.isfile(imageSequenceFolder + '/' + denoised):
                fileSeq = denoised.replace(seqNumbers, padding)
                unpaddedSeq = denoised
            else:
                fileSeq = files[0].replace(seqNumbers, padding)
                unpaddedSeq = files[0]
            imageSequencePath = os.path.join(imageSequenceFolder, fileSeq)
            unpaddedImageSequencePath = os.path.join(imageSequenceFolder, unpaddedSeq)
            return imageSequencePath, unpaddedImageSequencePath
        return None, None

    def populateReadNode(self, imageSequencePath, readNode=None, files=None, lockFormat=False) -> nuke.Node:
        global PROJECT_FORMAT
        if files is None:
            imageSequenceFolder = os.path.split(imageSequencePath)[0]
            files = self.listSequenceFiles(imageSequenceFolder)
        firstFrame = int(files[0].split('.')[-2])
        lastFrame = int(files[-1].split('.')[-2])

        if not readNode:
            readNode = nuke.createNode('Read')

        readNode['file'].setValue(imageSequencePath)
        readNode['first'].setValue(firstFrame)
        readNode['last'].setValue(lastFrame)
        readNode['origfirst'].setValue(firstFrame)
        readNode['origlast'].setValue(lastFrame)
        readNode['on_error'].setValue(3)

        if lockFormat:
            PROJECT_FORMAT = _registerProjectFormat(readNode.format())
            readNode['format'].setValue(PROJECT_FORMAT)
            nuke.root()['format'].setValue(PROJECT_FORMAT)

        return readNode

    def loadSequence(self, directory, lockFormat=False) -> nuke.Node:
        files = self.listSequenceFiles(directory)
        path, _ = self.createSeqPath(directory, files)
        if not path:
            nuke.warning('No EXR detected for' + directory)
        else:
            node = self.populateReadNode(path, files=files, lockFormat=lockFormat)
            return node

    def findExistingReadForPass(self, directory) -> nuke.Node:
        path, unpaddedPath = self.createSeqPath(directory)
        if not path:
            return None
        template = self.tk.template_from_path(unpaddedPath)
        fields = template.get_fields(unpaddedPath)
        del fields['version']
        del fields['SEQ']

        padding = "%04d"
        all_version_paths = list(set([".".join(x.split(".")[:-2]) + "." + padding + "." + x.split(".")[-1] for x in self.tk.paths_from_template(template,fields)]))

        for node in nuke.allNodes():
            if node.Class() == 'Read' and node.knob('file').value() in all_version_paths:
                return node
        return None

    def updateSequence(self, directory) -> nuke.Node:
        path, unpaddedPath = self.createSeqPath(directory)
        if not path:
            nuke.warning('No EXR detected for' + directory)
            return None
        existingRead = self.findExistingReadForPass(directory)
        if existingRead:
            return self.populateReadNode(path, existingRead)
        return None

    # ---------------------------------------------------------------------------- #
    #                              UI button actions                               #
    # ---------------------------------------------------------------------------- #
    def constructComp(self, version) -> nuke.Node:
        '''
        Builds the comp, including four main sections: pass build, lens treatment, output write, and viewer.
        '''
        temp_dot = nuke.createNode('Dot')
        x = temp_dot.xpos()
        y = temp_dot.ypos()
        nuke.delete(temp_dot)

        #-------------------------------------PASS BUILD-------------------------------------

        #AOV columns (read + distortion block + pass build, merged into one running merge tail)
        tails = self.buildPasses(version, x, y)
        mergeTail = tails.get('mergeTail')

        #Anchor the output block off of wherever the merge chain ends
        bx, by = (mergeTail.xpos(), mergeTail.ypos()) if mergeTail else (x, y)

        #-------------------------------------LENS TREATMENT-------------------------------------
        kisLensXpos = bx
        kisLensYpos = by + (mergeTail.screenHeight() if mergeTail else 0) + 50

        #-------------------------------------FINAL GRADE-------------------------------------
        gradeYpos = kisLensYpos + 80
        gradeBackdropHeight = 550 
        gradeBackdrop = nuke.nodes.BackdropNode(xpos=kisLensXpos - 210, ypos=gradeYpos, inputs=[])
        gradeBackdrop['bdwidth'].setValue(500.0)
        gradeBackdrop['bdheight'].setValue(gradeBackdropHeight)
        gradeBackdrop['tile_color'].setValue(676813311)
        gradeBackdrop['label'].setValue('FINAL GRADE')
        gradeBackdrop['note_font_size'].setValue(50.0)
        gradeBackdrop['note_font'].fromScript('Segoe UI Bold')
        gradeBackdrop['note_font_color'].setValue(4294967295)
        gradeBackdrop['z_order'].setValue(1.0)
        gradeBottomYpos = gradeYpos + gradeBackdropHeight

        #node_KisLensTreatments_1
        kisLensYpos = gradeBottomYpos + 150

        node_KisLensTreatments_1 = self.createLensTreatment(mergeTail, kisLensXpos, kisLensYpos)

        #-------------------------------------OUTPUT-------------------------------------

        outputXpos = kisLensXpos
        lensBottomYpos = kisLensYpos + node_KisLensTreatments_1.screenHeight() + 50
        outputYpos = lensBottomYpos + 150

        #node_Remove_1 (pre-crop channel cleanup)
        node_Remove_1 = nuke.nodes.Remove(xpos=outputXpos, ypos=outputYpos, inputs=[node_KisLensTreatments_1])
        node_Remove_1['operation'].fromScript('keep')
        node_Remove_1['channels'].fromScript('rgb')

        #node_Crop_1 (pre-write)
        node_Crop_1 = nuke.nodes.Crop(xpos=outputXpos, ypos=outputYpos+50, inputs=[node_Remove_1])
        node_Crop_1['box'].setValue((0.0, 0.0, PROJECT_FORMAT.width(), PROJECT_FORMAT.height()))
        node_Crop_1['reformat'].setValue(True)
        node_Crop_1['crop'].setValue(False)

        #node_WriteTank_1
        node_WriteTank_1 = nuke.nodes.WriteTank(xpos=outputXpos, ypos=outputYpos+100, inputs=[node_Crop_1])
        node_WriteTank_1['tank_channel'].fromScript('output')

        self.makeBackdrop(
            [node_Remove_1, node_Crop_1, node_WriteTank_1],
            'OUTPUT',
            tile_color=255
        )

        # Focus on the build.
        for n in nuke.allNodes():
            n['selected'].setValue(True)
        if nuke.GUI:
            nuke.zoomToFitSelected()

        return node_WriteTank_1

    def updateLighting(self, version) -> None:
        '''
        Repoints every existing Read node in the script that belongs to a lighting pass to the given
        version. Only updates existing nodes, it doesn't restructure the graph.
        '''
        passNames = ['bty'] + [passName for passName, _ in PASS_ORDER] + self.findExtraPasses(version)

        changed = False
        matched = False
        for passName in passNames:
            directory = os.path.join(self.publishes, version, passName, 'lightgroups', 'primary', 'exr')
            if not os.path.isdir(directory):
                continue
            existingRead = self.findExistingReadForPass(directory)
            if not existingRead:
                continue
            matched = True
            path, _ = self.createSeqPath(directory)
            if path and existingRead['file'].value() != path:
                self.populateReadNode(path, existingRead)
                changed = True

            if PASS_CONFIG.get(passName, {}).get('raw'):
                rawDirectory = os.path.join(self.publishes, version, passName + '_raw', 'lightgroups', 'primary', 'exr')
                if os.path.isdir(rawDirectory):
                    existingRawRead = next((n for n in nuke.allNodes('Read')
                        if n.knob('comp_pass') and n['comp_pass'].value() == passName
                        and n.knob('comp_role') and n['comp_role'].value() == 'raw_read'), None)
                    if existingRawRead:
                        rawPath, _ = self.createSeqPath(rawDirectory)
                        if rawPath and existingRawRead['file'].value() != rawPath:
                            self.populateReadNode(rawPath, existingRawRead)
                            changed = True

        if not matched:
            nuke.warning('No lighting Read nodes found in the script to update.')
        elif changed:
            nuke.message('Lighting has been updated to version ' + version + '.')
        else:
            nuke.message('Lighting is already up to date at version ' + version + '.')

    def bringInNewPasses(self) -> None:
        '''
        Entry point for the "Bring In New Passes" button. 
        Requires a selection before doing anything irreversible: if nothing's selected,
        it asks the compositor to select what needs to move and stops there.
        '''
        version = self.latest
        passNames = ['bty'] + [passName for passName, _ in PASS_ORDER] + self.findExtraPasses(version)

        unmatchedPasses = []
        for passName in passNames:
            directory = os.path.join(self.publishes, version, passName, 'lightgroups', 'primary', 'exr')
            if os.path.isdir(directory) and not self.findExistingReadForPass(directory):
                unmatchedPasses.append(passName)

        if not unmatchedPasses:
            nuke.message('No new passes found at version ' + version + '.')
            return

        passName = unmatchedPasses[0]
        remaining = unmatchedPasses[1:]
        remainingNote = ''
        if remaining:
            remainingNote = ('\n\n' + str(len(remaining)) + ' more new pass(es) also found ('
                + ', '.join(remaining) + '), only one is brought in per click. Run this again '
                + 'afterward for the next one and make sure you have the correct nodes selected.')

        if not nuke.selectedNodes():
            nuke.message('Next up: ' + passName + '. Select nodes that need to move first, then continue.' + remainingNote)
            return

        prompt = ('This will bring in a new pass: ' + passName + remainingNote
            + '\n\nThis action cannot be undone. Are you sure you want to insert?')
        if nuke.ask(prompt):
            self.insertNewPasses(version, [passName])

    def loadCamera(self):
        temp_Dot = nuke.createNode('Dot')
        x = temp_Dot.xpos()
        y = temp_Dot.ypos()

        shot = self.assetName
        sequence = shot.split('_')[0]
        querySuffix = "?Task=layout&Shot={}&Sequence={}&Step=layout&version=latest&Asset={}_cam&asset_type=camera"

        usdQuery = "tank:/" + projectCode + "/maya_publish_shot_asset_cache_usd" + querySuffix.format(shot, sequence, shot)
        alembicQuery = "tank:/" + projectCode + "/maya_publish_shot_asset_cache_alembic" + querySuffix.format(shot, sequence, shot)

        def resolveFilepath(query):
            try:
                path = resolver.uri_to_filepath(query)
            except Exception as e:
                print("Resolve failed:", e)
                return None
            return path if path and os.path.exists(path) else None
        
        def createUsdCameraNode(path):
            camera = nuke.nodes.Camera4()
            camera['import_enabled'].setValue(True)
            camera['file'].setValue(path)
            camera['import_prim_path'].setValue(f"/{shot}_cam") 
            camera['Reload'].execute()
            camera.setXpos(x)
            camera.setYpos(y)
            return camera
        
        def createAlembicCameraNode(path):
            camera = nuke.nodes.Camera2(read_from_file_link = True, file = path)
            camera.setXpos(x)
            camera.setYpos(y)
            return camera
        
        try:
            usdPath = resolveFilepath(usdQuery)
            if usdPath:
                return createUsdCameraNode(usdPath)
            
            alembicPath = resolveFilepath(alembicQuery)
            if alembicPath:
                return createAlembicCameraNode(alembicPath)

            nuke.warning("No valid USD or Alembic camera data found.")
            return None
        finally:
            nuke.delete(temp_Dot)

    # ---------------------------------------------------------------------------- #
    #                           Comp build orchestration                           #
    # ---------------------------------------------------------------------------- #
    def getPassFns(self, mergeYpos) -> dict:
        return {
            'envshd': lambda readNode, inputNode, xpos, ypos, columnLabel, distortionBackdrop, rawDataNodes: self.createShadowPass(readNode, inputNode, xpos, ypos, mergeYpos, columnLabel, distortionBackdrop, rawDataNodes=rawDataNodes),
            'ambient_occlusion': lambda readNode, inputNode, xpos, ypos, columnLabel, distortionBackdrop, rawDataNodes: self.createAoPass(readNode, inputNode, xpos, ypos, mergeYpos, columnLabel, distortionBackdrop, rawDataNodes=rawDataNodes),
            'env_refl_occlu': lambda readNode, inputNode, xpos, ypos, columnLabel, distortionBackdrop, rawDataNodes: self.createReflOPass(readNode, inputNode, xpos, ypos, mergeYpos, columnLabel, distortionBackdrop, rawDataNodes=rawDataNodes),
            'env_reflection': lambda readNode, inputNode, xpos, ypos, columnLabel, distortionBackdrop, rawDataNodes: self.createReflPass(readNode, inputNode, xpos, ypos, mergeYpos, columnLabel, distortionBackdrop, rawDataNodes=rawDataNodes),
        }

    def dispatchPass(self, version, passName, columnLabel, xpos, ypos, mergeYpos, passFns, lockFormat=False, puzzleMatteStart=None, buildDistortion=True) -> nuke.Node:
        '''
        Dispatches the pass build to either a custom setup function or the default pass block builder.
        Returns the pass's column tail, or None if it isn't on disk.
        '''
        passFn = passFns.get(passName)
        if passFn: # Custom setup function exists for this pass, build the shared front half, then hand off to the mapped function.
            frontHalf = self.buildColumnFrontHalf(version, passName, xpos, ypos, lockFormat=lockFormat, buildDistortion=buildDistortion)
            if frontHalf is None:
                return None
            readNode, distortionTail, distortionBackdrop, rawDataNodes = frontHalf
            return passFn(readNode, distortionTail, xpos, ypos, columnLabel, distortionBackdrop, rawDataNodes)
        else: # For passes that have no custom setup.
            return self.buildDefaultPassBlock(version, passName, columnLabel, xpos, ypos, mergeYpos, lockFormat=lockFormat, puzzleMatteStart=puzzleMatteStart, buildDistortion=buildDistortion)

    def buildColumnFrontHalf(self, version, passName, xpos, ypos, lockFormat=False, buildDistortion=True) -> tuple:
        '''
        Builds the Read node, optional finalBalance (main lane only), optional raw render data, and the
        distortion block. The distortion block is skipped there's no plate  in the build to match with.
        Returns (readNode, distortionTail, distortionBackdrop, rawDataNodes), or None if the pass isn't on disk.
        '''
        directory = os.path.join(self.publishes, version, passName, 'lightgroups', 'primary', 'exr')
        if not os.path.isdir(directory):
            return None
        readNode = self.loadSequence(directory, lockFormat=lockFormat)
        if not readNode:
            return None
        readNode.setXYpos(xpos, ypos - 100)
        self.tagPassNode(readNode, passName, PASS_CONFIG.get(passName, {}).get('lane'), role='column')

        layerContactSheet = nuke.nodes.LayerContactSheet(xpos=xpos+150, ypos=ypos-65, inputs=[readNode])
        layerContactSheet['showLayerNames'].setValue(True)

        distortionInput = readNode
        distortionYpos = ypos
        rawDataNodes = [layerContactSheet]
        if PASS_CONFIG.get(passName, {}).get('raw'):
            distortionInput, rawFromRead = self.addRawRenderData(version, passName, distortionInput, xpos, ypos)
            rawDataNodes += rawFromRead
            distortionYpos = distortionInput.ypos() + 50

        if buildDistortion:
            distortionTail, distortionBackdrop = self.createDistortionBlock(distortionInput, xpos, distortionYpos)
        else:
            distortionTail, distortionBackdrop = distortionInput, None
        return readNode, distortionTail, distortionBackdrop, rawDataNodes

    def connectMainPasses(self, columnTails, mergeYpos) -> nuke.Node:
        '''
        Chains the main-lane passes together via disjoint-over merges. 
        Returns the final link, or None if no main-lane pass was built.
        '''
        mainNames = [name for name, cfg in PASS_CONFIG.items() if cfg.get('lane') == 'main' and name in columnTails]
        if not mainNames:
            return None
        chainTail = None
        for passName in mainNames:
            tail = columnTails[passName]
            chainTail = self.mergePass(chainTail, tail, tail.xpos(), mergeYpos, operation='disjoint-over', label='DISJ OVER', passName=passName, premult=True)
            self.resizeAdjustBackdrop(passName, mergeYpos - 100)
        return chainTail

    def connectSecondaryPasses(self, plateNode, columnTails, mergeYpos) -> nuke.Node:
        '''
        Taps the secondary lane passes onto the plate one at a time, stacked top to bottom at same x-pos as the plate. 
        A Dot is placed at each pass's own column x-pos and the tap's y-pos.
        Returns the final link, or None if no secondary lane pass was built.
        '''
        knownSecondary = [name for name, cfg in PASS_CONFIG.items() if cfg.get('lane') == 'secondary' and name in columnTails]
        unknown = sorted(name for name in columnTails if name not in PASS_CONFIG)
        spineXpos = plateNode.xpos()
        tail = plateNode
        tapYpos = mergeYpos
        for passName in knownSecondary + unknown:
            cfg = PASS_CONFIG.get(passName, {})
            columnTail = columnTails[passName]
            elbowDot = nuke.nodes.Dot(xpos=columnTail.xpos()+34, ypos=tapYpos, inputs=[columnTail])

            extraBackdropNodes = None
            if passName == 'envshd':
                node_Constant_1 = nuke.nodes.Constant(xpos=spineXpos - 200, ypos=tapYpos - 100)
                node_Constant_1['color'].setValue(0.0)
                node_Constant_1['format'].setValue(PROJECT_FORMAT)

                nukescripts.clear_selection_recursive()
                node_Roto_1 = nuke.createNode('Roto', inpanel=False)
                node_Roto_1.setInput(0, node_Constant_1)
                node_Roto_1['xpos'].setValue(spineXpos - 200)
                node_Roto_1['ypos'].setValue(tapYpos)
                node_Roto_1['format'].setValue(PROJECT_FORMAT)
                rotoFormat = node_Roto_1.format()
                self.addRotoRectangle(node_Roto_1, (0, 0, rotoFormat.width(), rotoFormat.height()))
                extraBackdropNodes = [node_Constant_1, node_Roto_1]

            tail = self.mergePass(tail, elbowDot, spineXpos, tapYpos,
                operation=cfg.get('operation'), 
                mix=cfg.get('mix'), 
                indicators=cfg.get('indicators'),
                label=cfg.get('label', passName.upper()), 
                passName=passName, 
                sideBySide=True,
                extraBackdropNodes=extraBackdropNodes)
            self.resizeAdjustBackdrop(passName, tapYpos + 200)

            tapYpos += SECONDARY_TAP_SPACING
        return tail

    def buildPasses(self, version, startXpos, startYpos) -> dict:
        '''
        Builds every pass's column, then wires the two lanes together:
        1. Main lane's columns (left of the plate)
        2. BTY + PLATE (middle - the spine hangs straight down from the plate)
        3. Secondary lane's columns, plus any extra pass folders found on disk (right of the plate)
        4. Secondary lane taps onto the plate one at a time in order
        5. Main lane chains together in order
        6. Main-lane result taps onto the secondary-lane result once (CG OVER, a plain merge - no Remove)
        '''
        horizontalSpacing = 1500
        mergeYpos = startYpos + 2600
        tailNodes = {}
        xpos = startXpos
        passFns = self.getPassFns(mergeYpos)
        shouldLockFormat = True
        nextPuzzleMatteNum = 1

        # Resolve the plate directory up front, whether a plate exists determines whether any pass gets a distortion/overscan block at all 
        plateExrDir = None
        for plateFolder in ('plateClean01', 'plateRaw01'):
            plateDir = self.shotPath + f'/departments/comp/{plateFolder}/review/'
            if not os.path.isdir(plateDir):
                continue
            plateVersions = sorted((p for p in os.listdir(plateDir) if os.path.isdir(os.path.join(plateDir, p))), reverse=True)
            if not plateVersions:
                continue
            candidateExrDir = os.path.join(plateDir, plateVersions[0], 'exr')
            if os.path.isdir(candidateExrDir) and self.listSequenceFiles(candidateExrDir):
                plateExrDir = candidateExrDir
                break
        hasPlate = plateExrDir is not None

        mainPassOrder = [(name, cfg['label']) for name, cfg in PASS_CONFIG.items() if cfg.get('lane') == 'main']
        secondaryPassOrder = [(name, cfg['label']) for name, cfg in PASS_CONFIG.items() if cfg.get('lane') == 'secondary']

        extraPasses = self.findExtraPasses(version)
        secondaryPassOrder += [(p, p.upper()) for p in extraPasses]

        # MAIN PASS COLUMNS (left of the plate)
        for passName, columnLabel in mainPassOrder:
            wantsPuzzleMattes = PASS_CONFIG.get(passName, {}).get('puzzleMattes', False)
            tail = self.dispatchPass(version, passName, columnLabel, xpos, startYpos, mergeYpos, passFns,
                lockFormat=shouldLockFormat, puzzleMatteStart=nextPuzzleMatteNum if wantsPuzzleMattes else None,
                buildDistortion=hasPlate)
            if tail is not None:
                tailNodes[passName] = tail
                xpos += horizontalSpacing
                shouldLockFormat = False
                if wantsPuzzleMattes:
                    nextPuzzleMatteNum += 2

        # BEAUTY
        btyBackdrop = None
        btyDir = os.path.join(self.publishes, version, 'bty', 'lightgroups', 'primary', 'exr')
        if os.path.isdir(btyDir):
            btyNode = self.loadSequence(btyDir, lockFormat=shouldLockFormat)
            if btyNode:
                btyNode.setXYpos(xpos, startYpos - 100)
                tailNodes['bty'] = btyNode
                btyBackdrop = self.makeBackdrop([btyNode], 'BEAUTY', tile_color=777926143, z_order=0.0, note_font_size=80.0)
                shouldLockFormat = False

        # ENV (plate)
        plateNode = self.loadSequence(plateExrDir, lockFormat=shouldLockFormat) if plateExrDir else None
        if plateNode:
            shouldLockFormat = False
        plateLoaded = plateNode is not None
        plateYpos = int(round(btyBackdrop.ypos() + btyBackdrop['bdheight'].value() + 150)) if btyBackdrop else startYpos

        if plateLoaded:
            plateNode.setXYpos(xpos, plateYpos)
            plateNode['colorspace'].fromScript('scene_linear')
            plateNode['out_colorspace'].fromScript('ACEScg')
        else:
            # full-CG shots have no plate on disk so build with constant node instead
            plateNode = nuke.nodes.Constant(xpos=xpos, ypos=plateYpos, inputs=[])
            plateNode['color'].setValue(0.0)
            plateNode['format'].setValue(PROJECT_FORMAT)

        #node_Reformat_1 (plate)
        node_Reformat_1 = nuke.nodes.Reformat(xpos=xpos, ypos=plateYpos+120, inputs=[plateNode])
        node_Reformat_1['format'].setValue(PROJECT_FORMAT)
        node_Reformat_1['resize'].fromScript('none')
        node_Reformat_1['pbb'].setValue(True)

        self.makeBackdrop([plateNode, node_Reformat_1], 'PLATE' if plateLoaded else 'CONSTANT', tile_color=424758015, z_order=0.0, note_font_size=80.0)
        plateNode = node_Reformat_1

        tailNodes['plate'] = plateNode
        self.tagPassNode(plateNode, None, None, role='plate')
        xpos += horizontalSpacing

        # SECONDARY PASS COLUMNS (right of the plate)
        for passName, columnLabel in secondaryPassOrder:
            wantsPuzzleMattes = PASS_CONFIG.get(passName, {}).get('puzzleMattes', False)
            tail = self.dispatchPass(version, passName, columnLabel, xpos, startYpos, mergeYpos, passFns,
                lockFormat=shouldLockFormat, puzzleMatteStart=nextPuzzleMatteNum if wantsPuzzleMattes else None,
                buildDistortion=hasPlate)
            if tail is not None:
                tailNodes[passName] = tail
                xpos += horizontalSpacing
                shouldLockFormat = False
                if wantsPuzzleMattes:
                    nextPuzzleMatteNum += 2

        columnTails = {name: tail for name, tail in tailNodes.items() if tail is not None and name not in ('bty', 'plate')}

        '''
        WIRING: secondary lane taps the plate directly, main lane chains separately, then the two lanes merge together once at the end (CG OVER).
        '''
        secondaryTapCount = sum(1 for name in columnTails if PASS_CONFIG.get(name, {}).get('lane') != 'main')
        spineEndYpos = mergeYpos + SECONDARY_TAP_SPACING * secondaryTapCount
        mainLaneYpos = spineEndYpos + 300

        secondaryTail = self.connectSecondaryPasses(plateNode, columnTails, mergeYpos) if plateNode else None
        mainTail = self.connectMainPasses(columnTails, mainLaneYpos)

        if secondaryTail and mainTail:
            #node_Merge2 (CG OVER merge)
            mergeTail = nuke.nodes.Merge2(xpos=mainTail.xpos() + horizontalSpacing, ypos=mainTail.ypos(), inputs=[secondaryTail, mainTail])
            mergeTail['also_merge'].fromScript('all')
            self.makeBackdrop([mergeTail], 'CG OVER', tile_color=1077952767, z_order=1.0)
            self.tagPassNode(mergeTail, None, None, role='cg_over')
        else:
            mergeTail = secondaryTail or mainTail

        tailNodes['mergeTail'] = mergeTail
        return tailNodes

    def findExtraPasses(self, version) -> list:
        '''
        Finds any pass folders on disk for the given version that aren't part of PASS_ORDER.
        '''
        knownPassFolders = {passName for passName, _ in PASS_ORDER} | {'bty'} | EXCLUDED_PASS_NAMES
        versionDir = os.path.join(self.publishes, version)
        if not os.path.isdir(versionDir):
            return []
        return sorted(
            p for p in os.listdir(versionDir)
            if p not in knownPassFolders and '_raw' not in p and os.path.isdir(os.path.join(versionDir, p))
        )

    def buildDefaultPassBlock(self, version, passName, columnLabel, xpos, ypos, mergeYpos, lockFormat=False, puzzleMatteStart=None, buildDistortion=True) -> nuke.Node:
        '''
        Builds the default pass block (front half, no custom back half) for a pass with no custom
        setup. If puzzleMatteStart is given, also builds the puzlle matte block.
        Returns the column's tail node, or None if the pass isn't on disk.
        '''
        frontHalf = self.buildColumnFrontHalf(version, passName, xpos, ypos, lockFormat=lockFormat, buildDistortion=buildDistortion)
        if frontHalf is None:
            return None
        readNode, distortionTail, distortionBackdrop, rawDataNodes = frontHalf

        columnBackdrop = self.makeBackdrop([n for n in [readNode, distortionTail, distortionBackdrop] if n] + rawDataNodes, columnLabel, tile_color=2241535, z_order=0.0, note_font_size=80.0)
        if distortionBackdrop is None:
            columnBackdrop.setXpos(int(readNode.xpos() + readNode.screenWidth() / 2.0 - columnBackdrop['bdwidth'].value() / 2.0))
        tail = self.createAdjustPlaceholder(columnBackdrop, columnBackdrop.ypos()+columnBackdrop['bdheight'].value(), mergeYpos + SECONDARY_TAP_SPACING * 4, passName=passName, tailNode=distortionTail)
        if puzzleMatteStart is not None:
            self.createPuzzleMattes(readNode, passName, columnBackdrop.xpos()-50, columnBackdrop.ypos(), puzzleMatteStart)
        return tail

    def insertNewPasses(self, version, passNames) -> None:
        '''
        Builds and splices one new pass into the comp:
        1. bty: place above plate, unwired, done.
        2. find predecessor and successor via findLaneNeighbors
        3. guard: need a selection if anything must shift
        4. build the new column
        5. shift selected nodes to make room
        6. compute the new connector's position
        7. build and wire the new connector in
        '''
        horizontalSpacing = 1500
        passLabels = dict(PASS_ORDER)

        for passName in passNames:
            if passName == 'bty': # place above plate, no wiring needed
                directory = os.path.join(self.publishes, version, 'bty', 'lightgroups', 'primary', 'exr')
                btyNode = self.loadSequence(directory)
                if btyNode:
                    plateNode = next((n for n in nuke.allNodes()
                        if n.knob('comp_role') and n['comp_role'].value() == 'plate'), None)
                    if plateNode:
                        btyNode.setXYpos(plateNode.xpos(), plateNode.ypos())
                    else:
                        temp_dot = nuke.createNode('Dot')
                        btyNode.setXYpos(temp_dot.xpos(), temp_dot.ypos())
                        nuke.delete(temp_dot)

                    btyBackdrop = self.makeBackdrop([btyNode], 'BEAUTY', tile_color=777926143, z_order=0.0, note_font_size=80.0)

                    if plateNode:
                        plateBackdropTop = plateNode.ypos() - 250
                        deltaY = (plateBackdropTop - 50) - (btyBackdrop.ypos() + btyBackdrop['bdheight'].value())
                        btyNode.setYpos(int(round(btyNode.ypos() + deltaY)))
                        btyBackdrop.setYpos(int(round(btyBackdrop.ypos() + deltaY)))
                continue

            lane = PASS_CONFIG.get(passName, {}).get('lane', 'secondary')
            if lane == 'secondary':
                nuke.message(passName + ' is a secondary pass. Please build a new comp template with Build Output Nodes.')
                continue

            predecessor, successor = self.findLaneNeighbors(passName, lane)

            # whichever node currently sits right after the insertion point needs its input redirected onto the new connector, which is either:
            # 1. the next built pass in this lane,
            # 2. or CG OVER if we're appending past the last one (CG OVER isn't tagged as a lane connector, not showing up as successor, looked up separately)
            downstreamNode = successor
            downstreamInputIndex = 0
            if downstreamNode is None:
                downstreamNode = next((n for n in nuke.allNodes() if n.knob('comp_role') and n['comp_role'].value() == 'cg_over'), None)
                downstreamInputIndex = 1  # CG OVER's inputs are [secondary lane -> main lane]

            # need a selection to shift out of the way for the new connector
            selectedToShift = nuke.selectedNodes()
            if not selectedToShift:
                nuke.message(passName + ' needs room made for it but nothing is selected.\n\n'
                    'Select whatever should move (e.g. the successor pass, or CG OVER/LENS/OUTPUT '
                    'for an append), then run Update Lighting again. Skipped for now.')
                continue

            # column's x matches where its connector will land: 
            # predecessor + one column-width, 
            # or the successor's current position if there's no predecessor 
            # or the lane's rightmost column if the lane is empty so far
            laneColumns = [n for n in nuke.allNodes() if n.knob('comp_role') and n['comp_role'].value() == 'column' and n.knob('comp_lane') and n['comp_lane'].value() == lane]
            colYpos = int(min((n.ypos() for n in laneColumns), default=0))
            if predecessor:
                colXpos = int(predecessor.xpos()) + horizontalSpacing
            elif successor:
                colXpos = int(successor.xpos())
            else:
                colXpos = int(max((n.xpos() for n in laneColumns), default=0)) + horizontalSpacing
            mergeYpos = colYpos + 2600
            columnLabel = passLabels.get(passName, passName.upper())
            passFns = self.getPassFns(mergeYpos)

            # If this pass has puzzle matte outputs, figure out what number to start with for the new column's puzzle matte passes.
            puzzleMatteStart = None
            if PASS_CONFIG.get(passName, {}).get('puzzleMattes'):
                existingPuzzleMattePasses = sum(
                    1 for n in nuke.allNodes()
                    if n.knob('comp_role') and n['comp_role'].value() == 'column'
                    and n.knob('comp_pass') and PASS_CONFIG.get(n['comp_pass'].value(), {}).get('puzzleMattes')
                )
                puzzleMatteStart = existingPuzzleMattePasses * 2 + 1

            # no distortion block if the comp was built without a plate
            platePassNode = next((n for n in nuke.allNodes() if n.knob('comp_role') and n['comp_role'].value() == 'plate'), None)
            buildDistortion = not (platePassNode and platePassNode.input(0) and platePassNode.input(0).Class() == 'Constant')

            tail = self.dispatchPass(version, passName, columnLabel, colXpos, colYpos, mergeYpos, passFns,
                puzzleMatteStart=puzzleMatteStart, buildDistortion=buildDistortion)
            if tail is None:
                continue

            # shift selected nodes to make room for the new connector
            self.shiftNodes(selectedToShift, 'x', horizontalSpacing)

            # new connector sits one column-width right of predecessor, at predecessor's height
            insertXpos = predecessor.xpos() + horizontalSpacing if predecessor else tail.xpos()
            insertYpos = predecessor.ypos() - 50 if predecessor else mergeYpos

            # build and wire the new connector in
            newConnector = self.mergePass(predecessor, tail, insertXpos, insertYpos, operation='disjoint-over', label='DISJ OVER', passName=passName, premult=True)
            self.resizeAdjustBackdrop(passName, insertYpos - 100)
            if downstreamNode:
                downstreamNode.setInput(downstreamInputIndex, newConnector)

    def findLaneNeighbors(self, passName, lane) -> tuple:
        '''
        For inserting passName into the given lane, returns (predecessor, successor):
        -predecessor: the connector that the new pass's connector will be built on top of. 
        -successor: the existing connector that currently depends on predecessor and needs repointing onto the new passes connector. 
        '''
        connectors = {}
        for n in nuke.allNodes():
            if n.knob('comp_role') and n['comp_role'].value() == 'connector' \
                    and n.knob('comp_lane') and n['comp_lane'].value() == lane and n.knob('comp_pass'):
                connectors[n['comp_pass'].value()] = n

        laneOrder = [name for name, cfg in PASS_CONFIG.items() if cfg.get('lane') == lane]
        predecessor = successor = None
        if passName in laneOrder:
            index = laneOrder.index(passName)
            predecessorName = next((p for p in reversed(laneOrder[:index]) if p in connectors), None)
            successorName = next((p for p in laneOrder[index+1:] if p in connectors), None)
            predecessor = connectors.get(predecessorName)
            successor = connectors.get(successorName)

        if predecessor is None and successor is None and connectors:
            predecessor = max(connectors.values(), key=lambda n: n.xpos())

        return predecessor, successor

    def shiftNodes(self, nodes, axis, delta) -> None:
        '''
        Moves each of the selected nodes by delta units along the given axis ('x' or 'y').
        This is used to make room for a new pass's column when inserting mid-chain.
        '''
        for n in nodes:
            if axis == 'x':
                n.setXpos(int(n.xpos() + delta))
            else:
                n.setYpos(int(n.ypos() + delta))

    # ---------------------------------------------------------------------------- #
    #                       Shared reusable building blocks                        #
    # ---------------------------------------------------------------------------- #
    def makeBackdrop(self, nodes, label, tile_color=None, note_font_size=50.0, z_order=None, note_font='Segoe UI Bold', note_font_color=4294967295, width=None, height=None, padding=50.0, padding_top=100.0) -> nuke.Node:
        def nodeRight(n):
            if n.Class() == 'BackdropNode':
                return n.xpos() + n['bdwidth'].value()
            return n.xpos() + n.screenWidth()

        def nodeBottom(n):
            if n.Class() == 'BackdropNode':
                return n.ypos() + n['bdheight'].value()
            return n.ypos() + n.screenHeight()

        topPadding = padding if padding_top is None else padding_top

        minX = min(n.xpos() for n in nodes)
        minY = min(n.ypos() for n in nodes)
        maxX = max(nodeRight(n) for n in nodes)
        maxY = max(nodeBottom(n) for n in nodes)

        bdX = minX - padding
        bdY = minY - topPadding
        bdWidth = (maxX - minX) + padding * 2
        bdHeight = (maxY - minY) + topPadding + padding

        if bdWidth < 500.0:
            centerX = bdX + bdWidth / 2.0
            bdWidth = 500.0
            bdX = centerX - 250.0

        if width is not None:
            centerX = bdX + bdWidth / 2.0
            bdWidth = width
            bdX = centerX - width / 2.0

        if height is not None:
            bdHeight = height

        bd = nuke.nodes.BackdropNode(xpos=bdX, ypos=bdY, inputs=[])
        bd['bdwidth'].setValue(bdWidth)
        bd['bdheight'].setValue(bdHeight)
        if tile_color is not None:
            bd['tile_color'].setValue(tile_color)
        bd['label'].setValue(label)
        bd['note_font_size'].setValue(note_font_size)
        if z_order is not None:
            bd['z_order'].setValue(z_order)
        if note_font:
            bd['note_font'].fromScript(note_font)
        if note_font_color is not None:
            bd['note_font_color'].setValue(note_font_color)
        return bd

    def addRotoRectangle(self, rotoNode, box) -> None:
        '''
        Adds a simple 4-point rectangle shape to a Roto node's root layer.
        box = (x1, y1, x2, y2)
        '''
        curveKnob = rotoNode['curves']
        rootLayer = curveKnob.rootLayer

        shape = rp.Shape(curveKnob)
        x1, y1, x2, y2 = box
        for x, y in [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]:
            shape.append(rp.ShapeControlPoint(x, y))

        rootLayer.append(shape)
        curveKnob.changed()

    def addRawRenderData(self, version, passName, beautyReadNode, xpos, ypos) -> tuple[nuke.Node, list]:
        '''
        Unpremults the beauty read for raw render data shuffling.
        '''
        directory = os.path.join(self.publishes, version, passName + '_raw', 'lightgroups', 'primary', 'exr')
        if not os.path.isdir(directory):
            return beautyReadNode, []

        rawReadNode = self.loadSequence(directory)
        if not rawReadNode:
            return beautyReadNode, []
        rawReadNode['format'].setValue(PROJECT_FORMAT)
        rawReadNode['on_error'].setValue(3)
        self.tagPassNode(rawReadNode, passName, PASS_CONFIG.get(passName, {}).get('lane'), role='raw_read')
        addedNodes = []

        # Shuffle nodes - raw read is wired as B (input 1) on all three, in1/out1 both set to
        # the real layer name so the layer keeps its identity on output instead of landing in rgba.
        shuffle1Ypos = ypos + 50
        #node_Shuffle2_1 (CryptoMaterial)
        node_Shuffle2_1 = nuke.nodes.Shuffle2(xpos=xpos, ypos=shuffle1Ypos, inputs=[beautyReadNode, rawReadNode])
        node_Shuffle2_1.forceValidate()
        node_Shuffle2_1['in1'].fromScript('CryptoMaterial')
        node_Shuffle2_1['out1'].fromScript('CryptoMaterial')
        node_Shuffle2_1['fromInput1'].setValue('A')
        addedNodes.append(node_Shuffle2_1)

        shuffle2Ypos = shuffle1Ypos + 50
        #node_Shuffle2_2 (N)
        node_Shuffle2_2 = nuke.nodes.Shuffle2(xpos=xpos, ypos=shuffle2Ypos, inputs=[node_Shuffle2_1, rawReadNode])
        node_Shuffle2_2.forceValidate()
        node_Shuffle2_2['in1'].fromScript('N')
        node_Shuffle2_2['out1'].fromScript('N')
        node_Shuffle2_2['fromInput1'].setValue('A')
        node_Shuffle2_2['mappings'].setValue([
            (0, 'N.x', 'N.red'),
            (0, 'N.y', 'N.green'),
            (0, 'N.z', 'N.blue'),
        ])
        addedNodes.append(node_Shuffle2_2)

        rawReadNode.setXYpos(xpos + 150, shuffle2Ypos - 35)
        addedNodes.append(rawReadNode)

        shuffle3Ypos = shuffle2Ypos + 50
        #node_Shuffle2_3 (P)
        node_Shuffle2_3 = nuke.nodes.Shuffle2(xpos=xpos, ypos=shuffle3Ypos, inputs=[node_Shuffle2_2, rawReadNode])
        node_Shuffle2_3.forceValidate()
        node_Shuffle2_3['in1'].fromScript('P')
        node_Shuffle2_3['out1'].fromScript('P')
        node_Shuffle2_3['fromInput1'].setValue('A')
        node_Shuffle2_3['mappings'].setValue([
            (0, 'P.x', 'P.red'),
            (0, 'P.y', 'P.green'),
            (0, 'P.z', 'P.blue'),
        ])
        addedNodes.append(node_Shuffle2_3)

        readRight = rawReadNode.xpos() + rawReadNode.screenWidth()
        readBottom = rawReadNode.ypos() + rawReadNode.screenHeight()
        rawBackdrop = nuke.nodes.BackdropNode(xpos=rawReadNode.xpos() - 30, ypos=rawReadNode.ypos() - 50, inputs=[])
        rawBackdrop['bdwidth'].setValue((readRight + 30) - (rawReadNode.xpos() - 30))
        rawBackdrop['bdheight'].setValue((readBottom + 30) - (rawReadNode.ypos() - 50))
        rawBackdrop['label'].setValue('RAW ' + passName.upper())
        rawBackdrop['note_font'].fromScript('Bitstream Vera Sans Bold')
        rawBackdrop['note_font_size'].setValue(25.0)
        rawBackdrop['z_order'].setValue(24.0)

        return node_Shuffle2_3, addedNodes

    def createDistortionBlock(self, inputNode, xpos, ypos) -> tuple[nuke.Node, nuke.Node]:
        '''
        Builds the distortion/overscan block (Reformat -> LD_3DE4_Radial_Standard_Degree_4 -> Transform -> Crop). 
        Returns the tail node (Crop) and the backdrop that wraps the whole block.
        '''
        ypos += 130  

        #node_Reformat_1
        node_Reformat_1 = nuke.nodes.Reformat(xpos=xpos, ypos=ypos, inputs=[inputNode])
        node_Reformat_1['format'].setValue(PROJECT_FORMAT)
        node_Reformat_1['resize'].fromScript('none')
        node_Reformat_1['pbb'].setValue(True)

        #node_LD_3DE4_Radial_Standard_Degree_4_1
        node_LD_3DE4_Radial_Standard_Degree_4_1 = nuke.nodes.LD_3DE4_Radial_Standard_Degree_4(xpos=xpos, ypos=ypos+50, inputs=[node_Reformat_1])
        node_LD_3DE4_Radial_Standard_Degree_4_1['direction'].fromScript('distort')

        #node_Reformat_2
        node_Reformat_2 = nuke.nodes.Reformat(xpos=xpos, ypos=ypos+100, inputs=[node_LD_3DE4_Radial_Standard_Degree_4_1])
        node_Reformat_2['type'].fromScript('to format')

        #node_Crop_1
        node_Crop_1 = nuke.nodes.Crop(xpos=xpos, ypos=ypos+140, inputs=[node_Reformat_2])
        node_Crop_1['box'].setValue((0.0, 0.0, PROJECT_FORMAT.width(), PROJECT_FORMAT.height()))
        node_Crop_1['reformat'].setValue(True)
        node_Crop_1['crop'].setValue(False)

        distortionBackdrop = self.makeBackdrop(
            [node_Reformat_1, node_LD_3DE4_Radial_Standard_Degree_4_1, node_Reformat_2, node_Crop_1],
            'DISTORTION/OVERSCAN',
            tile_color=1226644991,
            z_order=1.0,
            note_font_size=40.0,
        )

        return node_Crop_1, distortionBackdrop

    def createPuzzleMattes(self, readNode, passName, rightEdgeXpos, ypos, startNum) -> nuke.Node:
        '''
        Builds the repeatable puzzle matte extraction block, connected directly off the Read node.
        Returns the backdrop that wraps the whole block, or None if this pass's PuzzleMatte
        channels aren't rendered yet, the pass can still be flagged puzzleMattes=True in
        PASS_CONFIG ahead of time without the block actually getting built until the channels show up.
        '''
        matteNum1, matteNum2 = startNum, startNum + 1
        namedLayer1, namedLayer2 = 'puzzle_{}_a'.format(passName), 'puzzle_{}_b'.format(passName)
        legacyLayer1, legacyLayer2 = 'PuzzleMatte_{:02d}'.format(matteNum1), 'PuzzleMatte_{:02d}'.format(matteNum2)

        layers = set(ch.split('.')[0] for ch in readNode.channels())
        if namedLayer1 in layers or namedLayer2 in layers:
            layer1, layer2 = namedLayer1, namedLayer2
            label1, label2 = '<center>a', '<center>b'
        elif legacyLayer1 in layers or legacyLayer2 in layers:
            layer1, layer2 = legacyLayer1, legacyLayer2
            label1, label2 = '<center>{:02d}'.format(matteNum1), '<center>{:02d}'.format(matteNum2)
        else:
            nuke.warning(readNode.name() + ': no ' + '/'.join([namedLayer1, namedLayer2, legacyLayer1, legacyLayer2]) + ' channels found, puzzle mattes not built.')
            return None

        xpos = 0
        dotYpos = ypos + 140

        #node_Dot_1 (connects to the read node)
        node_Dot_1 = nuke.nodes.Dot(xpos=xpos+34, ypos=dotYpos, inputs=[readNode])
        node_Dot_2 = nuke.nodes.Dot(xpos=xpos-336, ypos=dotYpos, inputs=[node_Dot_1])

        ypos = dotYpos + 120

        #layer 1
        node_Shuffle2_6 = nuke.nodes.Shuffle2(xpos=xpos, ypos=ypos, inputs=[node_Dot_1])
        node_Shuffle2_6['in1'].fromScript(layer1)

        node_Shuffle2_7 = nuke.nodes.Shuffle2(xpos=xpos-100, ypos=ypos+93, inputs=[node_Shuffle2_6])
        node_Shuffle2_7['mappings'].setValue([(0, 'rgba.red', 'rgba.red'), (-1, 'black', 'rgba.green'), (-1, 'black', 'rgba.blue'), (0, 'rgba.alpha', 'rgba.alpha')])
        node_Shuffle2_7['tile_color'].setValue(4278190335)
        node_Shuffle2_7['postage_stamp'].setValue(True)

        node_Shuffle2_5 = nuke.nodes.Shuffle2(xpos=xpos, ypos=ypos+93, inputs=[node_Shuffle2_6])
        node_Shuffle2_5['mappings'].setValue([(-1, 'black', 'rgba.red'), (0, 'rgba.green', 'rgba.green'), (-1, 'black', 'rgba.blue'), (0, 'rgba.alpha', 'rgba.alpha')])
        node_Shuffle2_5['tile_color'].setValue(536805631)
        node_Shuffle2_5['postage_stamp'].setValue(True)

        node_Shuffle2_1 = nuke.nodes.Shuffle2(xpos=xpos+100, ypos=ypos+93, inputs=[node_Shuffle2_6])
        node_Shuffle2_1['mappings'].setValue([(-1, 'black', 'rgba.red'), (-1, 'black', 'rgba.green'), (0, 'rgba.blue', 'rgba.blue'), (0, 'rgba.alpha', 'rgba.alpha')])
        node_Shuffle2_1['tile_color'].setValue(4194303)
        node_Shuffle2_1['postage_stamp'].setValue(True)

        backdrop01 = self.makeBackdrop(
            [node_Shuffle2_6],
            label1, tile_color=1717987071, z_order=2.0, note_font_size=30.0,
            width=152.0, padding=10.0, padding_top=70.0
        )

        #layer 2
        node_Shuffle2_8 = nuke.nodes.Shuffle2(xpos=xpos-370, ypos=ypos, inputs=[node_Dot_2])
        node_Shuffle2_8['in1'].fromScript(layer2)

        node_Shuffle2_4 = nuke.nodes.Shuffle2(xpos=xpos-470, ypos=ypos+93, inputs=[node_Shuffle2_8])
        node_Shuffle2_4['mappings'].setValue([(0, 'rgba.red', 'rgba.red'), (-1, 'black', 'rgba.green'), (-1, 'black', 'rgba.blue'), (0, 'rgba.alpha', 'rgba.alpha')])
        node_Shuffle2_4['tile_color'].setValue(4278190335)
        node_Shuffle2_4['postage_stamp'].setValue(True)

        node_Shuffle2_3 = nuke.nodes.Shuffle2(xpos=xpos-370, ypos=ypos+93, inputs=[node_Shuffle2_8])
        node_Shuffle2_3['mappings'].setValue([(-1, 'black', 'rgba.red'), (0, 'rgba.green', 'rgba.green'), (-1, 'black', 'rgba.blue'), (0, 'rgba.alpha', 'rgba.alpha')])
        node_Shuffle2_3['tile_color'].setValue(536805631)
        node_Shuffle2_3['postage_stamp'].setValue(True)

        node_Shuffle2_2 = nuke.nodes.Shuffle2(xpos=xpos-270, ypos=ypos+93, inputs=[node_Shuffle2_8])
        node_Shuffle2_2['mappings'].setValue([(-1, 'black', 'rgba.red'), (-1, 'black', 'rgba.green'), (0, 'rgba.blue', 'rgba.blue'), (0, 'rgba.alpha', 'rgba.alpha')])
        node_Shuffle2_2['tile_color'].setValue(4194303)
        node_Shuffle2_2['postage_stamp'].setValue(True)

        backdrop02 = self.makeBackdrop(
            [node_Shuffle2_8],
            label2, tile_color=1145324799, z_order=2.0, note_font_size=30.0,
            width=152.0, padding=10.0, padding_top=70.0
        )

        puzzleMattesBackdrop = self.makeBackdrop(
            [node_Dot_1, node_Dot_2, backdrop01, backdrop02, node_Shuffle2_7, node_Shuffle2_5, node_Shuffle2_1, node_Shuffle2_4, node_Shuffle2_3, node_Shuffle2_2],
            '<center>puzzleMattes', tile_color=572662527, z_order=1.0, note_font_size=80.0,
            padding_top=130.0
        )

        deltaX = rightEdgeXpos - (puzzleMattesBackdrop.xpos() + puzzleMattesBackdrop['bdwidth'].value())
        for node in [node_Dot_1, node_Dot_2, node_Shuffle2_6, node_Shuffle2_7, node_Shuffle2_5, node_Shuffle2_1,
                     node_Shuffle2_8, node_Shuffle2_4, node_Shuffle2_3, node_Shuffle2_2,
                     backdrop01, backdrop02, puzzleMattesBackdrop]:
            node.setXpos(int(round(node.xpos() + deltaX)))

        return puzzleMattesBackdrop

    def createAdjustPlaceholder(self, columnBackdrop, ypos, mergeYpos, passName=None, tailNode=None) -> nuke.Node:
        '''
        This builds a plain backdrop that doesn't wrap any nodes.
        There are no nodes to wrap here, this backdrop's size/position only needs to match columnBackdrop's exact xpos/width,
        and stretch down to above the merge block, so it's simpler to set those directly than to fake a node for
        makeBackdrop to measure.
        Tagged (role='adjust') when passName is given, so the connect step can find and resize it
        once this pass's actual connector position is known - the mergeYpos passed in here is often
        just a generous placeholder guess, not the final position.
        '''
        adjustBackdrop = nuke.nodes.BackdropNode(xpos=columnBackdrop.xpos(), ypos=ypos, inputs=[])
        adjustBackdrop['bdwidth'].setValue(columnBackdrop['bdwidth'].value())
        adjustBackdrop['bdheight'].setValue((mergeYpos-50) - ypos)
        adjustBackdrop['tile_color'].setValue(572662527)
        adjustBackdrop['label'].setValue('ADJUST')
        adjustBackdrop['z_order'].setValue(1.0)
        adjustBackdrop['note_font_size'].setValue(50.0)
        adjustBackdrop['note_font'].fromScript('Segoe UI Bold')
        adjustBackdrop['note_font_color'].setValue(4294967295)
        if passName:
            self.tagPassNode(adjustBackdrop, passName, PASS_CONFIG.get(passName, {}).get('lane'), role='adjust')

        if tailNode is not None:
            if PASS_CONFIG.get(passName, {}).get('lane') == 'main':
                #node_Unpremult_1
                node_Unpremult_1 = nuke.nodes.Unpremult(xpos=tailNode.xpos(), ypos=ypos+100, inputs=[tailNode])
                node_Unpremult_1['channels'].fromScript('all')
                #node_finalBalance_1
                node_finalBalance_1 = nuke.nodes.finalBalance(xpos=tailNode.xpos(), ypos=ypos+150, inputs=[node_Unpremult_1])
                return node_finalBalance_1
            return tailNode

        return adjustBackdrop

    def resizeAdjustBackdrop(self, passName, backdropBottomYpos) -> None:
        '''
        Shrinks/grows a pass's ADJUST placeholder, because secondary lane passes' layout makes each passes' adjust 
        backdrop ends up at different heights, so the ADJUST backdrop needs to be resized to match the new connector's 
        actual y position.
        '''
        adjustBackdrop = next((n for n in nuke.allNodes('BackdropNode')
            if n.knob('comp_pass') and n['comp_pass'].value() == passName
            and n.knob('comp_role') and n['comp_role'].value() == 'adjust'), None)
        if adjustBackdrop:
            adjustBackdrop['bdheight'].setValue(backdropBottomYpos - adjustBackdrop.ypos())

    def createLensTreatment(self, inputNode, xpos, ypos) -> nuke.Node:
        '''
        Builds the custom settings for the LENS TREATMENT block that sits right before the final output write.
        '''
        #node_KisLensTreatments_1
        node_KisLensTreatments_1 = nuke.nodes.KisLensTreatments(xpos=xpos, ypos=ypos, inputs=[inputNode])
        node_KisLensTreatments_1['indicators'].setValue(16.0)
        node_KisLensTreatments_1['disable'].setValue(True)

        self.makeBackdrop(
            [node_KisLensTreatments_1],
            'KIS LENS TREATMENT',
            tile_color=2131371775
        )

        return node_KisLensTreatments_1

    def tagPassNode(self, node, passName, lane, role='connector') -> nuke.Node:
        '''
        Adds hidden metadata knobs identifying which pass/lane/role a node belongs to, so a later-built pass can find it again by querying nuke.allNodes().
        - comp_pass: the pass name key from PASS_CONFIG, or an "extra" pass folder name found on disk. "None" for nodes not tied to one pass (plate, CG OVER).
        - comp_lane: 'main' or 'secondary', matching PASS_CONFIG's lane for that pass. "None" for lane-agnostic nodes (plate, CG OVER).
        - comp_role: what kind of node this is:
            - column: a pass's Read node, marking where its column starts (to identify where the new pass's column starts).
            - connector: the merge node that taps a pass into the chain in main lane or the middle spine for secondary passes (to identify where the new pass's connector should be built).
            - adjust: a pass's ADJUST placeholder backdrop (used by resizeAdjustBackdrop).
            - plate: the plate's Crop node, for secondary lane passes that comes after the plate.
            - cg_over: the CG OVER merge node, where main lane and secondary lane recombine.
            - raw_read: a pass's raw (CryptoMaterial/N/P) Read node, built in addRawRenderData for passes with raw=True (to identify it for version updates).
        '''
        for knobName, value in (('comp_pass', passName), ('comp_lane', lane), ('comp_role', role)):
            if value is None:
                continue
            if knobName not in node.knobs():
                knob = nuke.String_Knob(knobName)
                knob.setFlag(nuke.INVISIBLE)
                node.addKnob(knob)
            node[knobName].setValue(value)
        return node

    def mergePass(self, base, addition, xpos, ypos, operation=None, mix=None, indicators=None, label='OVER', draw_backdrop=True, columnBackdrop=None, passName=None, premult=False, sideBySide=False, extraBackdropNodes=None) -> nuke.Node:
        '''
        Creates the pass merging block.
        Essentially it builds Remove and Merge nodes, with the option to set the Merge operation, mix, and indicators.
        - premult=True inserts a Premult right before the Remove (for main passes)
        - sideBySide=True places the Remove to the right of the Merge (for secondary passes)
        - extraBackdropNodes lets a caller fold extra nodes into this block's backdrop
        '''
        source = addition
        if premult:
            #node_Premult_1
            node_Premult_1 = nuke.nodes.Premult(xpos=xpos, ypos=ypos-24, inputs=[addition])
            node_Premult_1['channels'].fromScript('all')
            source = node_Premult_1

        removeXpos = xpos + 150 if sideBySide else xpos
        #node_Remove_1
        node_Remove_1 = nuke.nodes.Remove(xpos=removeXpos, ypos=ypos + 20, inputs=[source])
        node_Remove_1['operation'].fromScript('keep')
        node_Remove_1['channels'].fromScript('rgba')
        node_Remove_1['channels2'].fromScript('depth')

        mergeYpos = ypos if sideBySide else ypos + 50
        # own content always at input 1, even with no base yet - so a later setInput(0, ...) to
        # attach a base never lands on the slot this pass's own content is sitting in.
        #node_Merge2_1
        node_Merge2_1 = nuke.nodes.Merge2(xpos=xpos, ypos=mergeYpos, inputs=[base, node_Remove_1])

        if operation:
            node_Merge2_1['operation'].fromScript(operation)
        node_Merge2_1['also_merge'].fromScript('all')
        if mix is not None:
            node_Merge2_1['mix'].setValue(mix)
        if indicators is not None:
            node_Merge2_1['indicators'].setValue(indicators)

        if draw_backdrop:
            backdropNodes = [node_Remove_1, node_Merge2_1] + (extraBackdropNodes or [])
            bd = self.makeBackdrop(backdropNodes, label, tile_color=1077952767, z_order=1.0)
            if columnBackdrop: # align the merge backdrop to the column backdrop's position and width
                bd.setXpos(columnBackdrop.xpos())
                bd['bdwidth'].setValue(columnBackdrop['bdwidth'].value())

        if passName:
            self.tagPassNode(node_Merge2_1, passName, PASS_CONFIG.get(passName, {}).get('lane'))

        return node_Merge2_1

    # ---------------------------------------------------------------------------------- #
    #                            Custom functions for passes                             #
    # ---------------------------------------------------------------------------------- #
    def createShadowPass(self, readNode, inputNode, xpos, ypos, mergeYpos, columnLabel, distortionBackdrop, rawDataNodes=None) -> nuke.Node:
        '''
        Builds the custom setup for the SHADOW (envshd) pass.
        '''
        ypos += 450

        #node_Shuffle2_1
        node_Shuffle2_1 = nuke.nodes.Shuffle2(xpos=xpos, ypos=ypos, inputs=[inputNode])
        node_Shuffle2_1.forceValidate()
        node_Shuffle2_1['in1'].fromScript('holdout_shadow')

        #node_Invert_1
        node_Invert_1 = nuke.nodes.Invert(xpos=xpos, ypos=ypos+41, inputs=[node_Shuffle2_1])

        shuffInvBackdrop = self.makeBackdrop([node_Shuffle2_1, node_Invert_1], 'SHUFF/INV', tile_color=572662527, z_order=1.0)

        columnBackdrop = self.makeBackdrop(
            [n for n in [readNode, inputNode, distortionBackdrop, shuffInvBackdrop] if n] + (rawDataNodes or []),
            columnLabel,
            tile_color=3352319,
            z_order=0.0,
            note_font_size=80.0
        )
        if distortionBackdrop is None:
            columnBackdrop.setXpos(int(readNode.xpos() + readNode.screenWidth() / 2.0 - columnBackdrop['bdwidth'].value() / 2.0))

        self.createAdjustPlaceholder(columnBackdrop, columnBackdrop.ypos()+columnBackdrop['bdheight'].value(), mergeYpos + SECONDARY_TAP_SPACING * 4, passName='envshd')

        return node_Invert_1

    def createAoPass(self, readNode, inputNode, xpos, ypos, mergeYpos, columnLabel, distortionBackdrop, rawDataNodes=None) -> nuke.Node:
        '''
        Builds the custom setup for the AO (ambient_occlusion) pass.
        '''
        ypos += 700

        #node_Histogram_1
        node_Histogram_1 = nuke.nodes.Histogram(xpos=xpos, ypos=ypos+87, inputs=[inputNode])
        node_Histogram_1['in'].setValue([0.12, 1.0, 1.0])

        #node_Constant_1
        node_Constant_1 = nuke.nodes.Constant(xpos=xpos+200, ypos=ypos, inputs=[])
        node_Constant_1['color'].setValue(1.0)
        node_Constant_1['format'].setValue(PROJECT_FORMAT)

        #node_Constant_2 (Roto input)
        node_Constant_2 = nuke.nodes.Constant(xpos=xpos+400, ypos=ypos)
        node_Constant_2['channels'].fromScript('rgba')
        node_Constant_2['color'].setValue(0.0)
        node_Constant_2['format'].setValue(PROJECT_FORMAT)

        #node_Roto_1
        nukescripts.clear_selection_recursive()
        node_Roto_1 = nuke.createNode('Roto', inpanel=False)
        node_Roto_1['xpos'].setValue(xpos+400)
        node_Roto_1['ypos'].setValue(ypos+80)
        node_Roto_1.setInput(0, node_Constant_2)
        node_Roto_1['format'].setValue(PROJECT_FORMAT)
        rotoFormat = node_Roto_1.format()
        self.addRotoRectangle(node_Roto_1, (0, 0, rotoFormat.width(), rotoFormat.height()))

        #node_Merge2_1 (Histogram over Constant, masked by the Roto placeholder)
        node_Merge2_1 = nuke.nodes.Merge2(xpos=xpos+200, ypos=ypos+87, inputs=[node_Constant_1, node_Histogram_1, node_Roto_1])

        maskBackdrop = self.makeBackdrop(
            [node_Histogram_1, node_Merge2_1, node_Roto_1, node_Constant_1, node_Constant_2],
            'MASK',
            tile_color=572662527,
            z_order=1.0,
            padding_top=300.0,
        )

        columnBackdrop = self.makeBackdrop(
            [n for n in [readNode, inputNode, distortionBackdrop, maskBackdrop] if n] + (rawDataNodes or []),
            columnLabel,
            tile_color=3352319,
            z_order=0.0,
            note_font_size=80.0
        )
        if distortionBackdrop is None:
            columnBackdrop.setXpos(int(readNode.xpos() + readNode.screenWidth() / 2.0 - columnBackdrop['bdwidth'].value() / 2.0))

        self.createAdjustPlaceholder(columnBackdrop, columnBackdrop.ypos()+columnBackdrop['bdheight'].value(), mergeYpos + SECONDARY_TAP_SPACING * 4, passName='ambient_occlusion')

        return node_Merge2_1

    def createReflOPass(self, readNode, inputNode, xpos, ypos, mergeYpos, columnLabel, distortionBackdrop, rawDataNodes=None) -> nuke.Node:
        '''
        Builds the custom setup for the REFL O (env_reflection_occlu) pass
        '''
        ypos += 450

        #node_Shuffle2_1
        node_Shuffle2_1 = nuke.nodes.Shuffle2(xpos=xpos, ypos=ypos, inputs=[inputNode])
        node_Shuffle2_1['mappings'].setValue([(-1, 'black', 'rgba.red'), (-1, 'black', 'rgba.green'), (-1, 'black', 'rgba.blue'), (0, 'rgba.red', 'rgba.alpha')])

        redAlphaBackdrop = self.makeBackdrop([node_Shuffle2_1], 'RED > ALPHA', tile_color=572662527, z_order=1.0)

        columnBackdrop = self.makeBackdrop(
            [n for n in [readNode, inputNode, distortionBackdrop, redAlphaBackdrop] if n] + (rawDataNodes or []),
            columnLabel,
            tile_color=3352319,
            z_order=0.0,
            note_font_size=80.0
        )
        if distortionBackdrop is None:
            columnBackdrop.setXpos(int(readNode.xpos() + readNode.screenWidth() / 2.0 - columnBackdrop['bdwidth'].value() / 2.0))

        self.createAdjustPlaceholder(columnBackdrop, columnBackdrop.ypos()+columnBackdrop['bdheight'].value(), mergeYpos + SECONDARY_TAP_SPACING * 4, passName='env_refl_occlu')

        return node_Shuffle2_1

    def createReflPass(self, readNode, inputNode, xpos, ypos, mergeYpos, columnLabel, distortionBackdrop, rawDataNodes=None) -> nuke.Node:
        '''
        Builds the custom setup for the REFL (env_reflection) pass. No treatment between
        distortion and merge - the column tail is the distortion tail itself.
        '''
        columnBackdrop = self.makeBackdrop(
            [n for n in [readNode, inputNode, distortionBackdrop] if n] + (rawDataNodes or []),
            columnLabel,
            tile_color=3352319,
            z_order=0.0,
            note_font_size=80.0
        )
        if distortionBackdrop is None:
            columnBackdrop.setXpos(int(readNode.xpos() + readNode.screenWidth() / 2.0 - columnBackdrop['bdwidth'].value() / 2.0))

        self.createAdjustPlaceholder(columnBackdrop, columnBackdrop.ypos()+columnBackdrop['bdheight'].value(), mergeYpos + SECONDARY_TAP_SPACING * 4, passName='env_reflection')

        return inputNode

class CompBuilderKarma(QtWidgets.QWidget):
    def __init__(self, parent = None):
        QtWidgets.QWidget.__init__(self, parent)
        self.loader = Loader()
        self.setWindowFlags(QtCore.Qt.WindowStaysOnTopHint)
        self.makeUI()
            
    def divider(self) -> QtWidgets.QWidget:
        """ Nuke divider """
        div = QtWidgets.QLabel ('')
        div.setStyleSheet ("QLabel {background-color: #4b4b4b; padding: 0; margin: 0; border-bottom: 1 solid #4b4b4b; border-top: 1 solid #191919;}")
        div.setMaximumHeight (2)
        return div

    def instructionLabel(self, text) -> QtWidgets.QWidget:
        label = QtWidgets.QLabel(text)
        label.setWordWrap(True)
        return label

    def instructionsSection(self) -> QtWidgets.QWidget:
        """ 
        Instruction section above the buttons.
        """
        container = QtWidgets.QWidget()
        container.setStyleSheet("font-size: 11pt;")
        sectionLayout = QtWidgets.QVBoxLayout()
        sectionLayout.setContentsMargins(0, 0, 0, 0)
        container.setLayout(sectionLayout)

        buttonNameStyle = 'color:#5DADE2; font-weight:bold;'

        sectionLayout.addWidget(QtWidgets.QLabel('<b>Instructions</b>'))
        sectionLayout.addWidget(self.instructionLabel(
            '<span style="' + buttonNameStyle + '">Construct Comp:</span> Construct the comp template at the selected lighting version.'))
        sectionLayout.addWidget(self.instructionLabel(
            '<span style="' + buttonNameStyle + '">Update Lighting to Selected Version:</span> Repoints existing passes to the selected lighting version only, it won\'t build or move anything.'))
        sectionLayout.addWidget(self.instructionLabel(
            '<span style="' + buttonNameStyle + '">Bring In New Passes:</span> Repeat the following process for each newly found passes:<br>'
            '1. See where the new pass fits in the existing comp<br>'
            '2. Select the nodes that need to move to make room for it<br>'
            '3. Click the button to bring in the new pass'))

        return container

    def onLockVersionToggled(self, checked):
        """ When locked, greys out every version except the latest (index 0) so it can't be selected. """
        model = self.versionMenu.model()
        for i in range(model.rowCount()):
            model.item(i).setEnabled(not checked or i == 0)
        if checked:
            self.versionMenu.setCurrentIndex(0)

    def onRefreshVersions(self):
        currentSelection = self.versionMenu.currentText()
        self.loader.refreshVersions()
        self.versionMenu.clear()
        self.versionMenu.addItems(self.loader.versions)
        if not self.lockVersionCheckbox.isChecked() and currentSelection in self.loader.versions:
            self.versionMenu.setCurrentText(currentSelection)
        self.onLockVersionToggled(self.lockVersionCheckbox.isChecked())

    def makeUI(self):
        self.layout = QtWidgets.QVBoxLayout()
        self.setWindowTitle("Comp Builder Karma")
        self.setObjectName("CompBuilderKarma")
        self.setLayout(self.layout)
        self.sgHeader = QtWidgets.QLabel('<b>Shotgrid Info</b>')
        self.currentShot = QtWidgets.QLabel('Current Shot: ' + self.loader.assetName)
        self.frameRange = QtWidgets.QLabel('Frame Range: ' + self.loader.startFrame + '-' + self.loader.endFrame)

        self.versionMenu = QtWidgets.QComboBox()
        self.versionMenu.addItems(self.loader.versions)
        self.versionMenu.setStyleSheet("font-size: 11pt;")

        self.lockVersionCheckbox = QtWidgets.QCheckBox('Lock to Latest')
        self.lockVersionCheckbox.setChecked(True)
        self.lockVersionCheckbox.toggled.connect(self.onLockVersionToggled)
        self.lockVersionCheckbox.setStyleSheet("font-size: 11pt;")

        self.refreshVersionsBtn = QtWidgets.QPushButton('Refresh')
        self.refreshVersionsBtn.clicked.connect(self.onRefreshVersions)
        self.refreshVersionsBtn.setStyleSheet("font-size: 11pt;")

        widgets1 = [self.sgHeader, self.currentShot, self.frameRange]
        for w in widgets1:
            w.setStyleSheet("font-size: 11pt;")
            self.layout.addWidget(w)

        versionRow = QtWidgets.QHBoxLayout()
        versionRow.addWidget(self.versionMenu)
        versionRow.addWidget(self.lockVersionCheckbox)
        versionRow.addWidget(self.refreshVersionsBtn)

        options = QtWidgets.QFormLayout()
        versionRowLabel = QtWidgets.QLabel('Lighting Versions: ')
        versionRowLabel.setStyleSheet("font-size: 11pt;")
        options.addRow(versionRowLabel, versionRow)
        self.layout.addLayout(options)

        self.onLockVersionToggled(self.lockVersionCheckbox.isChecked())

        self.layout.addWidget(self.divider())

        self.layout.addWidget(self.instructionsSection())

        self.buildOutputBtn = QtWidgets.QPushButton('Construct Comp')
        self.buildOutputBtn.clicked.connect(lambda: self.loader.constructComp(self.versionMenu.currentText()))
        self.buildOutputBtn.setStyleSheet("font-size: 11pt;")
        self.layout.addWidget(self.buildOutputBtn)
        self.updateLightingBtn = QtWidgets.QPushButton('Update Lighting to Selected Version')
        self.updateLightingBtn.clicked.connect(lambda: self.loader.updateLighting(self.versionMenu.currentText()))
        self.updateLightingBtn.setStyleSheet("font-size: 11pt;")
        self.layout.addWidget(self.updateLightingBtn)
        self.bringInNewPassesBtn = QtWidgets.QPushButton('Bring In New Passes')
        self.bringInNewPassesBtn.clicked.connect(self.loader.bringInNewPasses)
        self.bringInNewPassesBtn.setStyleSheet("font-size: 11pt;")
        self.layout.addWidget(self.bringInNewPassesBtn)

        self.addCamera = QtWidgets.QPushButton('Import Camera')
        self.addCamera.clicked.connect(self.loader.loadCamera)
        self.addCamera.setStyleSheet("font-size: 11pt;")
        self.layout.addWidget(self.addCamera)
        self.layout.addStretch()
        return

def compBuilderKarma():
    nuke.CompBuilderKarma = CompBuilderKarma
    if nuke.root().name().split(os.sep)[-1] == 'comp_latest.nk':
        nuke.alert("Save your file first!")
        return
    if sgtk.platform.current_engine().context.entity:
        try:
            pane = nuke.getPaneFor('Properties.1')
            panels.registerWidgetAsPanel('nuke.CompBuilderKarma', 'Comp Builder Karma', 'ala.compBuilderKarma', True).addToPane(pane)
        except IndexError:
            nuke.alert("Unable to open builder. Check if your shot has renders available!")
        except Exception as e:
            print(e)
            nuke.alert("Unable to open builder." + str(e))
    else:
        nuke.alert("Unable to open builder. Make sure you're in a Shotgrid context first!")