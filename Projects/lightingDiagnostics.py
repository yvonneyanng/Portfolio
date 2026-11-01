"""
@yvonne.yang
Builds diagnostic Nuke graphs for lighting review, lightingDiagnostics() for VFX shots, cgLightingDiagnostics() for CG shots. 
Both discover rendered passes on disk and lay out the same general sections: 
Slap Comp, LPEs, Passes, and Balls (VFX) / Oracle Variance Map (CG).

Key customization points:
addChannelsCustomSetup / addSlapCompCustomSetup: per-VFX-pass treatment for a Channels tile or a Slap Comp merge input.
if passes change, update:
- Update the AUX_PASSES dictionary with new aux pass names.
- Update CG_PASSES with any new CG passes and whether each gets puzzle mattes, there might be more keys added in the future for other per-pass customization.

"""

import json
import math
import os
import re
from pathlib import Path

import nuke
import sgtk
import shotgun_utils
from nukescripts import panels

if nuke.NUKE_VERSION_MAJOR < 11:
    from PySide import QtCore, QtWidgets
elif nuke.NUKE_VERSION_MAJOR < 16:
    from PySide2 import QtCore, QtWidgets
else:
    from PySide6 import QtCore, QtWidgets

CG_PASSES = {
    'char':    {'puzzleMattes': True},
    'monster': {'puzzleMattes': True},
    'char01':  {'puzzleMattes': True},
    'char02':  {'puzzleMattes': True},
    'char03':  {'puzzleMattes': True},
    'char04':  {'puzzleMattes': True},
    'prop':    {'puzzleMattes': True},
    'env':     {'puzzleMattes': True},
    'fx':      {'puzzleMattes': False},
    'fog':     {'puzzleMattes': False},
    'volume':  {'puzzleMattes': False},
}
EXCLUDED_PASS_NAMES = {'bty', 'ambient_occlusion_feet', 'macbethprobe'}

AUX_PASSES = {
    'ambient_occlusion': {'label': '<center>ao', 'operation': 'multiply'},
    'envshd':            {'label': '<center>shdw', 'operation': 'multiply'},
    'env_refl_occlu':    {'label': '<center>refl o', 'operation': 'over'},
    'env_reflection':    {'label': '<center>refl', 'operation': 'screen'},
}

READ_NODE_TILE_COLOR = 3014898687

PROJECT_FORMAT = next((f for f in nuke.formats() if f.name() == '4K_DCP'), None)
if PROJECT_FORMAT is None:
    nuke.warning('4K_DCP format not found, falling back to the current root format.')
    PROJECT_FORMAT = nuke.root().format()

BUILD_GAP_X = 200.0


# ---------------------------------------------------------------------------- #
#                                Layout helpers                                #
# ---------------------------------------------------------------------------- #
def _nodeRightEdge(node) -> float:
    if node.Class() == 'BackdropNode':
        return node.xpos() + node['bdwidth'].value()
    return node.xpos() + node.screenWidth()


def _shiftBuildToAvoidOverlap(newNodes, existingNodes, gap=BUILD_GAP_X) -> None:
    '''
    To avoid two graphs overlapping if both are built in the same comp, computing from live graph.
    '''
    if not newNodes or not existingNodes:
        return

    minX = min(n.xpos() for n in newNodes)
    existingMaxX = max(_nodeRightEdge(n) for n in existingNodes)

    dx = (existingMaxX + gap) - minX
    if dx > 0:
        for n in newNodes:
            n.setXpos(int(n.xpos() + dx))


_SEQUENCE_FRAME_RE = re.compile(r'\.\d+\.[^./]+$')
_FRAME_NUMBER_RE = re.compile(r'\.(\d+)\.[^./]+$')

def oracleStatsText(nodeName, frame):
    readNode = nuke.toNode(nodeName)
    if readNode is None:
        return ''

    try:
        raw = readNode.metadata('exr/husk:render_stats', frame)
        stats = json.loads(raw or '{}')
        settings = stats.get('render_settings') or {}
        oracle = json.loads(settings.get('pixeloracle') or '[]')
    except (ValueError, TypeError, AttributeError) as e:
        return ''

    variance = None
    if (
        isinstance(oracle, list)
        and len(oracle) > 1
        and isinstance(oracle[1], dict)
    ):
        variance = oracle[1].get('variance')

    fields = (
        ('Pixel Samples', settings.get('samplesperpixel')),
        ('Threshold', variance),
    )
    text = '\n'.join(
        f'{label}: {value}'
        for label, value in fields
        if value is not None
    )
    return text


_sequenceMaxUsageCache = {}

def sequenceMaxUsageText(nodeName):
    readNode = nuke.toNode(nodeName)
    if readNode is None:
        return ''

    filePath = readNode['file'].value()
    cacheKey = (nodeName, filePath)
    if cacheKey in _sequenceMaxUsageCache:
        return _sequenceMaxUsageCache[cacheKey]

    directory = os.path.dirname(filePath)
    frames = []
    if os.path.isdir(directory):
        for fileName in sorted(os.listdir(directory)):
            match = _FRAME_NUMBER_RE.search(fileName)
            if match:
                frames.append(int(match.group(1)))

    maxRam = None
    maxRamText = None
    maxTime = None
    maxTimeText = None

    for frame in frames:
        try:
            raw = readNode.metadata('exr/husk:render_stats', frame)
            stats = json.loads(raw or '{}')
        except (ValueError, TypeError):
            continue

        ram = (stats.get('system_memory') or {}).get('peak')
        wallclock = (stats.get('system_time') or {}).get('wallclock')

        if ram is not None and (maxRam is None or ram > maxRam):
            maxRam = ram
            maxRamText = readNode.metadata('exr/renderMemory_s', frame)

        if wallclock is not None and (maxTime is None or wallclock > maxTime):
            maxTime = wallclock
            timeText = readNode.metadata('exr/renderTime_s', frame)
            maxTimeText = str(timeText)[:-3] if timeText else None

    parts = []
    if maxRamText:
        parts.append(str(maxRamText))
    if maxTimeText:
        parts.append(maxTimeText)
    text = 'Max: ' + ' | '.join(parts) if parts else ''

    if text:
        _sequenceMaxUsageCache[cacheKey] = text
    return text


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

    def populateReadNode(self, imageSequencePath, files=None, readNode=None) -> nuke.Node:
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

        return readNode

    def loadSequence(self, directory) -> nuke.Node:
        files = self.listSequenceFiles(directory)
        path, _ = self.createSeqPath(directory, files)
        if not path:
            nuke.warning('No EXR detected for' + directory)
        else:
            return self.populateReadNode(path, files)

    def findExistingReadForPass(self, directory) -> nuke.Node:
        path, unpaddedPath = self.createSeqPath(directory)
        if not path:
            return None
        template = self.tk.template_from_path(unpaddedPath)
        if not template:
            return None
        fields = template.get_fields(unpaddedPath)
        fields.pop('version', None)
        fields.pop('SEQ', None)

        padding = "%04d"
        all_version_paths = list(set([".".join(x.split(".")[:-2]) + "." + padding + "." + x.split(".")[-1] for x in self.tk.paths_from_template(template, fields)]))

        for node in nuke.allNodes():
            if node.Class() == 'Read' and node.knob('file').value() in all_version_paths:
                return node
        return None

    def swapMacbethPlaceholder(self, path, files) -> bool:
        '''
        Finds the macbeth placeholder Constant built by createBalls when macbeth wasn't
        rendered yet, and replaces it with a real Read node.
        Returns True if a placeholder was found and swapped.
        '''
        placeholder = None
        for node in nuke.allNodes('Constant'):
            if node.name().startswith('MacbethPlaceholder'):
                placeholder = node
                break
        if not placeholder:
            return False

        readNode = self.populateReadNode(path, files)
        readNode.setXYpos(placeholder.xpos(), placeholder.ypos())
        _applyDiagnosticsReadSettings(readNode)

        for dependent in placeholder.dependent(nuke.INPUTS, forceEvaluate=False):
            for i in range(dependent.inputs()):
                if dependent.input(i) == placeholder:
                    dependent.setInput(i, readNode)

        nuke.delete(placeholder)

        for node in nuke.allNodes('Text2'):
            if node['message'].value() == 'Macbeth is shy...':
                node['message'].setValue('Balls')

        return True

    def updateLighting(self, version) -> None:
        versionDir = os.path.join(self.publishes, version)
        passNames = ['macbethprobe'] + [name + '_raw' for name in CG_PASSES]
        if os.path.isdir(versionDir):
            passNames += [
                passName for passName in sorted(os.listdir(versionDir))
                if not passName.endswith('_raw') and passName not in EXCLUDED_PASS_NAMES
            ]

        changed = False
        matched = False
        for passName in passNames:
            directory = os.path.join(self.publishes, version, passName, 'lightgroups', 'primary', 'exr')
            if not os.path.isdir(directory):
                continue
            existingRead = self.findExistingReadForPass(directory)
            if existingRead:
                matched = True
                path, _ = self.createSeqPath(directory)
                if path and existingRead['file'].value() != path:
                    self.populateReadNode(path, readNode=existingRead)
                    changed = True
            elif passName == 'macbethprobe':
                files = self.listSequenceFiles(directory)
                path, _ = self.createSeqPath(directory, files)
                if path and self.swapMacbethPlaceholder(path, files):
                    matched = True
                    changed = True

        if not matched:
            nuke.warning('No lighting Read nodes found in the script to update.')
            return

        if changed:
            nuke.message('Lighting has been updated to version ' + version + '.')
        else:
            nuke.message('Lighting is already up to date at version ' + version + '.')


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


def _applyDiagnosticsReadSettings(readNode, lockFormat=False) -> nuke.Node:
    '''
    Standard knob settings shared by every Read node in this comp.
    '''
    global PROJECT_FORMAT
    readNode['file_type'].fromScript('exr')
    if lockFormat:
        PROJECT_FORMAT = _registerProjectFormat(readNode.format())
        readNode['format'].setValue(PROJECT_FORMAT)
        nuke.root()['format'].setValue(PROJECT_FORMAT)
    else:
        readNode['format'].setValue(PROJECT_FORMAT)
    readNode['colorspace'].fromScript('scene_linear')
    readNode['out_colorspace'].fromScript('ACEScg')
    return readNode


class LightingDiagnostics():
    def __init__(self):
        self.loader = Loader()
        self.reads = {}
        self.cgReads = {}
        self.cgTileNodes = {}
        self.auxTileNodes = {}

    # ---------------------------------------------------------------------------- #
    #                                 Data loading                                 #
    # ---------------------------------------------------------------------------- #
    def loadPasses(self, xpos, ypos, passFilter=None) -> dict:
        reads = {}
        readSpacing = 110
        readXpos = xpos
        shouldLockFormat = True

        if passFilter is None or 'plate' in passFilter:
            plateExrDir = self.findLatestPlateExrDir()
            if plateExrDir:
                plateNode = self.loader.loadSequence(plateExrDir)
                if plateNode:
                    plateNode.setXYpos(readXpos, ypos)
                    reads['plate'] = _applyDiagnosticsReadSettings(plateNode, lockFormat=shouldLockFormat)
                    shouldLockFormat = False
                    readXpos += readSpacing

        versionDir = os.path.join(self.loader.publishes, self.loader.latest)
        if os.path.isdir(versionDir):
            for passName in sorted(os.listdir(versionDir)):
                if passName.endswith('_raw') or passName in EXCLUDED_PASS_NAMES:
                    continue
                if passFilter is not None and passName not in passFilter:
                    continue
                directory = os.path.join(versionDir, passName, 'lightgroups', 'primary', 'exr')
                if os.path.isdir(directory):
                    readNode = self.loader.loadSequence(directory)
                    if readNode:
                        readNode.setXYpos(readXpos, ypos)
                        reads[passName] = _applyDiagnosticsReadSettings(readNode, lockFormat=shouldLockFormat)
                        shouldLockFormat = False
                        readXpos += readSpacing

        self.reads = reads
        return reads

    def findLatestPlateExrDir(self) -> str:
        '''
        Resolves the latest version's exr dir from plateClean01, falling back to plateRaw01 if there's no usable plate found. 
        Returns None if neither has one.
        '''
        for plateFolder in ('plateClean01', 'plateRaw01'):
            plateDir = self.loader.shotPath + f'/departments/comp/{plateFolder}/review/'
            if not os.path.isdir(plateDir):
                continue
            plateVersions = sorted((p for p in os.listdir(plateDir) if os.path.isdir(os.path.join(plateDir, p))), reverse=True)
            if not plateVersions:
                continue
            plateExrDir = os.path.join(plateDir, plateVersions[0], 'exr')
            if os.path.isdir(plateExrDir) and self.loader.listSequenceFiles(plateExrDir):
                return plateExrDir
        return None

    def loadFreshPassRead(self, passName, xpos, ypos, raw=False) -> nuke.Node:
        '''
        Loads a fresh Read for passName from the latest version dir, positions
        it, and applies the standard diagnostics settings. 
        Returns None if the pass isn't found on disk.
        '''
        folderName = passName + '_raw' if raw else passName
        directory = os.path.join(self.loader.publishes, self.loader.latest, folderName, 'lightgroups', 'primary', 'exr')
        hasRawFolder = os.path.isdir(directory) and self.loader.listSequenceFiles(directory)
        if raw and not hasRawFolder:
            directory = os.path.join(self.loader.publishes, self.loader.latest, passName, 'lightgroups', 'primary', 'exr')
        if not os.path.isdir(directory):
            return None
        readNode = self.loader.loadSequence(directory)
        if not readNode:
            return None
        readNode.setXYpos(xpos, ypos)
        return _applyDiagnosticsReadSettings(readNode)

    def createRawUsageBlock(self, xpos, ypos) -> dict:
        '''
        Builds a raw Read per pass, for the resource usage information.
        Returns {passName: {'read': rawReadNode, 'maxLabel': baked 'RAM Max: x | Wall Clock Max: y'}}
        for attachUsageLabels to wire a live per-frame readout and the sequence max onto the Passes section's tile labels.
        '''
        passSpacing = 110
        passXpos = xpos
        rawReads = []
        usageData = {}

        for passName in list(CG_PASSES) + list(AUX_PASSES):
            readNode = self.loadFreshPassRead(passName, passXpos, ypos, raw=True)
            if not readNode:
                continue
            rawReads.append(readNode)
            passXpos += passSpacing

            usageData[passName] = {
                'read': readNode,
                'maxLabel': (
                    "[python {__import__('lightingDiagnostics').sequenceMaxUsageText(%r)}]"
                    % readNode.fullName()
                ),
            }

        if rawReads:
            self.makeBackdrop(
                rawReads,
                '<center>raw', tile_color=676813311, z_order=-2.0, note_font_size=80.0
            )

        return usageData

    def attachUsageLabels(self, usageData) -> None:
        '''
        Appends each pass's resource usage as extra lines onto that pass's tile label, which is live per-frame RAM/time and sequence max.
        '''
        preferences = nuke.toNode('preferences')
        expressionArrows = preferences.knob('expression_arrows') if preferences else None
        if expressionArrows:
            expressionArrows.setValue(False)

        for passName, data in usageData.items():
            tileNode = self.cgTileNodes.get(passName) or self.auxTileNodes.get(passName)
            if not tileNode:
                continue
            textNode = tileNode.input(0)
            if not textNode:
                continue

            rawReadName = data['read'].name()
            liveLine = (
                "[python {'Current: ' + "
                "(lambda n: str(n.metadata('exr/renderMemory_s', nuke.frame()) or '')"
                " + ' | ' + str(n.metadata('exr/renderTime_s', nuke.frame()) or '')[:-3])"
                "(nuke.toNode('%s'))"
                "}]"
                % rawReadName
            )

            lines = [textNode['message'].getValue(), liveLine]
            if data['maxLabel']:
                lines.append(data['maxLabel'])
            message = '\n'.join(lines)

            textNode['message'].fromScript(message)
            textNode['cursor_position'].setValue(len(message))
            if passName == 'ambient_occlusion':
                textNode['color'].setValue([0, 0, 0, 1])

    # ---------------------------------------------------------------------------- #
    #                       Shared reusable building blocks                        #
    # ---------------------------------------------------------------------------- #
    def makeBackdrop(self, nodes, label, tile_color, note_font_size=50.0, z_order=None, note_font='Segoe UI Bold', note_font_color=4294967295, width=None, height=None, padding=50.0, padding_top=100.0) -> nuke.Node:
        def nodeRight(n):
            if n.Class() == 'BackdropNode':
                return n.xpos() + n['bdwidth'].value()
            return n.xpos() + n.screenWidth()

        def nodeBottom(n):
            if n.Class() == 'BackdropNode':
                return n.ypos() + n['bdheight'].value()
            return n.ypos() + n.screenHeight()

        baseTopPadding = padding if padding_top is None else padding_top
        topPadding = max(baseTopPadding, note_font_size * 2.0)

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
            bdHeight = height + (topPadding - baseTopPadding)

        plainLabel = re.sub(r'<[^>]+>', '', label)
        minLabelWidth = len(plainLabel) * note_font_size * 0.4375 + padding * 2
        if bdWidth < minLabelWidth:
            centerX = bdX + bdWidth / 2.0
            bdWidth = minLabelWidth
            bdX = centerX - bdWidth / 2.0

        bd = nuke.nodes.BackdropNode(xpos=bdX, ypos=bdY, inputs=[])
        bd['bdwidth'].setValue(bdWidth)
        bd['bdheight'].setValue(bdHeight)
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

    def centeredXpos(self, nodes) -> float:
        '''
        Returns the x position that centers a new node under the horizontal span of `nodes`.
        '''
        return (min(n.xpos() for n in nodes) + max(n.xpos() for n in nodes)) / 2.0

    def restPassOrder(self, passNames) -> list:
        '''
        Orders a set of VFX pass names.
        '''
        passNames = set(passNames)
        known = [name for name in AUX_PASSES if name not in CG_PASSES and name in passNames]
        unknown = sorted(name for name in passNames if name not in AUX_PASSES)
        return known + unknown

    def styleTextLabel(self, text, message, box=None, titleLabel=False, color=None, xjustify=None) -> nuke.Node:
        text.forceValidate()
        frameHeight = text.format().height()
        frameWidth = text.format().width()

        if 'font_size' in text.knobs():
            if titleLabel:
                text['font_size'].setExpression('height * 0.02')
            else:
                text['font_size'].setExpression('height * 0.05')

        text['message'].fromScript(message)
        text['cursor_position'].setValue(len(message))
        if box is None:
            if titleLabel:
                box = (0.0, 0.0, frameWidth, 101.0)
            else:
                box = (0.0, frameHeight - 101.0, frameWidth, frameHeight)
        text['box'].setValue(box)
        if color is not None:
            text['color'].setValue(color)

        if titleLabel:
            if 'xjustify' in text.knobs():
                text['xjustify'].fromScript('left')
            if 'yjustify' in text.knobs():
                text['yjustify'].fromScript('bottom')
        if xjustify is not None and 'xjustify' in text.knobs():
            text['xjustify'].fromScript(xjustify)
        return text

    def createNamedLayerTiles(self, inputNode, prefix, centerXpos, ypos) -> list:
        '''
        Builds the Shuffle2 + Text2 tiles for each named layer in inputNode that starts with specific prefix.
        '''
        layers = sorted(set(channel.split('.')[0] for channel in inputNode.channels()))
        namedLayers = [layer for layer in layers if layer.startswith(prefix)]

        tileSpacing = 150
        rowWidth = (len(namedLayers) - 1) * tileSpacing if namedLayers else 0
        tiles = []
        tileXpos = centerXpos - rowWidth / 2.0
        for layerName in namedLayers:
            shuffle = nuke.nodes.Shuffle2(xpos=tileXpos, ypos=ypos, inputs=[inputNode])
            shuffle['in1'].fromScript(layerName)

            label = layerName[len(prefix):].capitalize()
            text = self.styleTextLabel(nuke.nodes.Text2(xpos=tileXpos, ypos=ypos + 24, inputs=[shuffle]), label, xjustify='right')

            tiles.append(text)
            tileXpos += tileSpacing

        return tiles

    def buildContactSheet(self, nodes, message, xpos, ypos, roworder=None, colorder=None, titleLabel=False, width=None, height=None, gap=0.0) -> nuke.Node:
        '''
        Wraps nodes into a ContactSheet with a text label.
        Returns the label node.
        '''
        if not nodes:
            return None

        columns = math.ceil(math.sqrt(len(nodes)))
        rows = math.ceil(len(nodes) / columns)
        sheetXpos = self.centeredXpos(nodes)

        contactSheet = nuke.nodes.ContactSheet(xpos=sheetXpos, ypos=ypos, inputs=nodes)
        if width is not None:
            contactSheet['width'].setValue(width)
        if height is not None:
            contactSheet['height'].setValue(height)
        contactSheet['rows'].setValue(rows)
        contactSheet['columns'].setValue(columns)
        contactSheet['gap'].setValue(gap)
        contactSheet['center'].setValue(True)
        contactSheet['startframe'].setValue(1.0)
        contactSheet['endframe'].setValue(1.0)
        if roworder:
            contactSheet['roworder'].fromScript(roworder)
        if colorder:
            contactSheet['colorder'].fromScript(colorder)

        text = self.styleTextLabel(nuke.nodes.Text2(xpos=sheetXpos, ypos=ypos + 24, inputs=[contactSheet]), message, titleLabel=titleLabel)

        return text

    def labelAndCleanTile(self, inputNode, message, xpos, ypos, color=None, box=None) -> nuke.Node:
        '''
        Builds a Text2 label + Remove node for Channels section.
        '''
        text = self.styleTextLabel(nuke.nodes.Text2(xpos=xpos, ypos=ypos, inputs=[inputNode]), message, box=box, color=color, xjustify='right')

        remove = nuke.nodes.Remove(xpos=xpos, ypos=ypos + 24, inputs=[text])
        remove['operation'].fromScript('keep')
        remove['channels'].fromScript('rgb')
        remove['channels2'].fromScript('alpha')

        return remove

    def createDistortionBlock(self, inputNode, xpos, ypos, flip=False) -> nuke.Node:
        '''
        Builds the shot's lens distortion block.
        Returns the tail node (Crop).
        '''
        step = -1 if flip else 1

        #node_Reformat_1
        node_Reformat_1 = nuke.nodes.Reformat(xpos=xpos, ypos=ypos + step*130, inputs=[inputNode])
        node_Reformat_1['format'].setValue(PROJECT_FORMAT)
        node_Reformat_1['resize'].fromScript('none')
        node_Reformat_1['pbb'].setValue(True)

        #node_LD_3DE4_Radial_Standard_Degree_4_1
        node_LD_3DE4_Radial_Standard_Degree_4_1 = nuke.nodes.LD_3DE4_Radial_Standard_Degree_4(xpos=xpos, ypos=ypos + step*180, inputs=[node_Reformat_1])
        node_LD_3DE4_Radial_Standard_Degree_4_1['direction'].fromScript('distort')

        #node_Reformat_2
        node_Reformat_2 = nuke.nodes.Reformat(xpos=xpos, ypos=ypos + step*230, inputs=[node_LD_3DE4_Radial_Standard_Degree_4_1])
        node_Reformat_2['type'].fromScript('to format')

        #node_Crop_1
        node_Crop_1 = nuke.nodes.Crop(xpos=xpos, ypos=ypos + step*270, inputs=[node_Reformat_2])
        node_Crop_1['box'].setValue((0.0, 0.0, PROJECT_FORMAT.width(), PROJECT_FORMAT.height()))
        node_Crop_1['reformat'].setValue(True)
        node_Crop_1['crop'].setValue(False)

        return node_Crop_1

    # ---------------------------------------------------------------------------- #
    #                            Per-pass custom setup                             #
    # ---------------------------------------------------------------------------- #
    def addChannelsCustomSetup(self, inputNode, passName, xpos, ypos) -> nuke.Node:
        if passName == 'envshd':
            shuffle = nuke.nodes.Shuffle2(xpos=xpos, ypos=ypos + 100, inputs=[inputNode])
            shuffle.forceValidate()
            shuffle['in1'].fromScript('holdout_shadow')
            return shuffle
        if passName == 'ambient_occlusion':
            histogram = nuke.nodes.Histogram(xpos=xpos, ypos=ypos + 100, inputs=[inputNode])
            histogram['in'].setValue([0.12, 0.7697747057, 1.0])

            mergeYpos = ypos + 150
            constant = nuke.nodes.Constant(xpos=xpos + 150, ypos=mergeYpos, inputs=[])
            constant['channels'].fromScript('rgba')
            constant['color'].setValue(1.0)
            constant['format'].setValue(PROJECT_FORMAT)

            merge = nuke.nodes.Merge2(xpos=xpos, ypos=mergeYpos, inputs=[constant, histogram])
            constant.setYpos(int(merge.ypos() + (merge.screenHeight() - constant.screenHeight()) / 2))
            return merge
        return inputNode

    def addSlapCompCustomSetup(self, inputNode, passName, xpos, ypos) -> nuke.Node:
        if passName == 'env_refl_occlu':
            shuffle = nuke.nodes.Shuffle2(xpos=xpos, ypos=ypos, inputs=[inputNode])
            shuffle['mappings'].setValue([(-1, 'black', 'rgba.red'), (-1, 'black', 'rgba.green'), (-1, 'black', 'rgba.blue'), (0, 'rgba.red', 'rgba.alpha')])
            return shuffle
        if passName == 'ambient_occlusion':
            histogram = nuke.nodes.Histogram(xpos=xpos, ypos=ypos, inputs=[inputNode])
            histogram['in'].setValue([0.12, 0.7697747057, 1.0])

            mergeYpos = ypos - 50
            constant = nuke.nodes.Constant(xpos=xpos + 150, ypos=mergeYpos, inputs=[])
            constant['channels'].fromScript('rgba')
            constant['color'].setValue(1.0)
            constant['format'].setValue(PROJECT_FORMAT)

            merge = nuke.nodes.Merge2(xpos=xpos, ypos=mergeYpos, inputs=[constant, histogram])
            constant.setYpos(int(merge.ypos() + (merge.screenHeight() - constant.screenHeight()) / 2))
            return merge
        if passName == 'envshd':
            shuffle = nuke.nodes.Shuffle2(xpos=xpos, ypos=ypos, inputs=[inputNode])
            shuffle.forceValidate()
            shuffle['in1'].fromScript('holdout_shadow')

            invert = nuke.nodes.Invert(xpos=xpos, ypos=ypos - 50, inputs=[shuffle])

            grade = nuke.nodes.Grade(xpos=xpos, ypos=ypos - 100, inputs=[invert])
            grade['white'].setValue([1.003332, 0.993332, 1.003332, 1.0])
            grade['white_panelDropped'].setValue(True)

            hueshift = nuke.nodes.HueShift(xpos=xpos, ypos=ypos - 150, inputs=[grade])
            hueshift['saturation'].setValue(0.22)
            hueshift['color_saturation'].setValue(1.75)

            histogram = nuke.nodes.Histogram(xpos=xpos, ypos=ypos - 200, inputs=[hueshift])
            histogram['in'].setValue([0.29, 1.0, 1.0])
            return histogram
        return inputNode

    # ---------------------------------------------------------------------------- #
    #                               Section builders                               #
    # ---------------------------------------------------------------------------- #
    def createLPEs(self, xpos, ypos) -> nuke.Node:
        '''
        Builds the LPE section: brings in env/char/prop, merge them, extracts a labeled tile per 'C_' layer, and wraps everything in a contact sheet.
        Returns the contact sheet's label node.
        '''
        passSpacing = 260
        passXpos = xpos
        passBackdrops = []

        for passName in CG_PASSES:
            if passName not in self.reads:
                continue
            readNode = self.reads[passName]
            readNode.setXYpos(passXpos, ypos)
            passBackdrops.append(self.makeBackdrop(
                [readNode], '<center>' + passName, tile_color=READ_NODE_TILE_COLOR, z_order=-1.0, note_font_size=80.0, width=240.0, height=220.0
            ))
            passXpos += passSpacing

        mergeYpos = ypos + 300
        mergeInputs = [self.reads[name] for name in CG_PASSES if name in self.reads]
        if not mergeInputs:
            return None

        mergeXpos = self.centeredXpos(mergeInputs)
        merge = mergeInputs[0]
        for node in mergeInputs[1:]:
            merge = nuke.nodes.Merge2(xpos=mergeXpos, ypos=mergeYpos, inputs=[merge, node])
            merge['maskChannelMask'].fromScript('none')
            merge['also_merge'].fromScript('all')

        tiles = self.createNamedLayerTiles(merge, 'C_', mergeXpos, mergeYpos + 100)
        contactSheetLabel = self.buildContactSheet(
            tiles, 
            'LPEs', 
            mergeXpos, 
            mergeYpos + 300,
            roworder='TopBottom',
            colorder='LeftRight',
            width=PROJECT_FORMAT.width(),
            height=PROJECT_FORMAT.height(),
            gap=0.0
        )

        self.makeBackdrop(
            [n for n in passBackdrops + [merge] + tiles + [contactSheetLabel] if n],
            '<center>LPEs', tile_color=1495869951, z_order=-2.0, note_font_size=80.0
        )

        return contactSheetLabel

    def createBalls(self, xpos, ypos) -> nuke.Node:
        '''
        Builds the Balls section
        Returns the label node.
        '''
        # macbeth probe is now rendered in every version, same layout as the other passes
        ballDir = os.path.join(self.loader.publishes, self.loader.latest, 'macbethprobe', 'lightgroups', 'primary', 'exr')
        ballNode = None
        if os.path.isdir(ballDir):
            ballNode = self.loader.loadSequence(ballDir)
            if ballNode:
                ballNode.setXYpos(xpos, ypos)
                _applyDiagnosticsReadSettings(ballNode)
                self.reads['macbethprobe'] = ballNode

        macbethMissing = not ballNode
        if macbethMissing:
            # macbeth not rendered yet at this version, build the section anyway with a placeholder Constant
            ballNode = nuke.nodes.Constant(xpos=xpos, ypos=ypos, channels='rgba', color=0.0)
            ballNode.setName('MacbethPlaceholder')
            ballNode['label'].setValue('Macbeth is shy...')
            ballNode['format'].setValue(PROJECT_FORMAT)

        if 'plate' not in self.reads:
            return None

        plateNode = self.reads['plate']
        plateNode.setXYpos(xpos + 300, ypos)

        ballBackdrop = self.makeBackdrop(
            [ballNode], '<center>balls', tile_color=READ_NODE_TILE_COLOR, z_order=-1.0, note_font_size=80.0, width=240.0, height=260.0
        )
        plateBackdrop = self.makeBackdrop(
            [plateNode], '<center>plate', tile_color=READ_NODE_TILE_COLOR, z_order=-1.0, note_font_size=80.0, width=250.0, height=260.0
        )

        distortedBall = self.createDistortionBlock(ballNode, xpos, ypos+100)

        mergeYpos = distortedBall.ypos() + 60
        mergeXpos = self.centeredXpos([plateNode, distortedBall])
        merge = nuke.nodes.Merge2(xpos=mergeXpos, ypos=mergeYpos, inputs=[plateNode, distortedBall])

        tileLabel = 'Macbeth is shy...' if macbethMissing else 'Balls'
        text = self.styleTextLabel(nuke.nodes.Text2(xpos=mergeXpos, ypos=mergeYpos + 24, inputs=[merge]), tileLabel)

        self.makeBackdrop(
            [ballBackdrop, plateBackdrop, merge, text],
            '<center>balls', tile_color=675764735, z_order=-2.0, note_font_size=80.0
        )

        return text

    def createVFXPasses(self, xpos, ypos) -> nuke.Node:
        '''
        Builds the Passes section: Puzzle Matte tile, one tile per pass, combined into a contact sheet.
        Returns the label node.
        '''
        tileYpos = ypos + 500
        sectionGap = 200.0

        beforePuzzle = set(nuke.allNodes())
        puzzleTile = self.createPuzzleMatteTile(xpos, ypos, tileYpos)
        restXpos = int(max((_nodeRightEdge(n) for n in set(nuke.allNodes()) - beforePuzzle), default=xpos) + sectionGap)

        beforeRest = set(nuke.allNodes())
        restTiles = self.createVFXChannelsRestPasses(restXpos, ypos, tileYpos)
        cgXpos = int(max((_nodeRightEdge(n) for n in set(nuke.allNodes()) - beforeRest), default=restXpos) + sectionGap)

        cgTiles = self.createCGTile(cgXpos, ypos, tileYpos)

        tiles = [tile for tile in [puzzleTile] + cgTiles + restTiles if tile]
        return self.buildContactSheet(
            tiles, 
            'Passes', 
            xpos, 
            ypos + 700, 
            roworder='TopBottom',
            colorder='LeftRight',
            width=PROJECT_FORMAT.width(),
            height=PROJECT_FORMAT.height(),
            gap=0.0
        )

    def createCGPasses(self, xpos, ypos) -> nuke.Node:
        '''
        Builds CG's Passes section.
        Returns the label node.
        '''
        tileYpos = ypos + 500
        sectionGap = 200.0

        beforePuzzle = set(nuke.allNodes())
        puzzleTile = self.createPuzzleMatteTile(xpos, ypos, tileYpos)
        cgXpos = int(max((_nodeRightEdge(n) for n in set(nuke.allNodes()) - beforePuzzle), default=xpos) + sectionGap)

        cgTiles = self.createCGTile(cgXpos, ypos, tileYpos)

        tiles = [tile for tile in [puzzleTile] + cgTiles if tile]
        return self.buildContactSheet(
            tiles,
            'Passes',
            xpos,
            ypos + 700,
            roworder='TopBottom',
            colorder='LeftRight',
            width=PROJECT_FORMAT.width(),
            height=PROJECT_FORMAT.height(),
            gap=0.0
        )

    def createVFXSlapComp(self, xpos, ypos) -> nuke.Node:
        '''
        Builds the Slap Comp section.
        Returns the 'Slap Comp' label node.
        '''
        plateExrDir = self.findLatestPlateExrDir()
        plateNode = None
        if plateExrDir:
            plateNode = self.loader.loadSequence(plateExrDir)
            if plateNode:
                _applyDiagnosticsReadSettings(plateNode)

        if not plateNode:
            return None

        verticalLift = 300
        mergeGap = 200

        excludedPasses = {'plate', 'macbethprobe'}
        otherPasses = self.restPassOrder(name for name in self.reads if name not in excludedPasses and name not in CG_PASSES)
        foregroundPasses = [name for name in CG_PASSES if name != 'env' and name in self.cgReads]
        passOrder = (['env'] if 'env' in self.cgReads else []) + otherPasses + foregroundPasses

        if not passOrder:
            return None

        distortedNodes = {}
        for passName in passOrder:
            sourceNode = self.cgReads[passName] if passName in CG_PASSES else self.reads[passName]
            distorted = self.createDistortionBlock(
                sourceNode, sourceNode.xpos(), sourceNode.ypos() - verticalLift, flip=True
            )
            distortedNodes[passName] = self.addSlapCompCustomSetup(distorted, passName, distorted.xpos(), distorted.ypos() - 180)

        cropYpositions = [node.ypos() for node in distortedNodes.values()]
        mergeYpos = min(cropYpositions) - mergeGap

        plateNode.setXYpos(xpos + 400, mergeYpos - 35)
        self.makeBackdrop(
            [plateNode], '<center>plate', tile_color=READ_NODE_TILE_COLOR, z_order=-1.0, note_font_size=80.0, width=250.0, height=260.0
        )

        base = plateNode
        lastMergeXpos = xpos
        for passName in passOrder:
            distorted = distortedNodes.get(passName)
            if not distorted:
                continue
            lastMergeXpos = (plateNode.xpos() + 150) if passName == 'env' else distorted.xpos()
            base = nuke.nodes.Merge2(xpos=lastMergeXpos, ypos=mergeYpos, inputs=[base, distorted])
            if passName in CG_PASSES:
                operation = 'disjoint-over'
            else:
                operation = AUX_PASSES.get(passName, {}).get('operation', 'over')
            base['operation'].fromScript(operation)

        text = self.styleTextLabel(nuke.nodes.Text2(xpos=base.xpos() + 400, ypos=mergeYpos, inputs=[base]), 'Slap Comp')

        return text

    def createCGSlapComp(self) -> nuke.Node:
        '''
        Builds CG's Slap Comp. No distortion/overscan block here.
        Returns the 'Slap Comp' label node.
        '''
        passNames = [name for name in CG_PASSES if name in self.cgReads]
        if not passNames:
            return None

        mergeGap = 500

        sourceNodes = {passName: self.cgReads[passName] for passName in passNames}
        mergeYpos = min(node.ypos() for node in sourceNodes.values()) - mergeGap

        firstNode = sourceNodes[passNames[0]]
        lastMergeXpos = firstNode.xpos()
        base = nuke.nodes.Merge2(xpos=lastMergeXpos, ypos=mergeYpos, inputs=[firstNode])
        base['maskChannelMask'].fromScript('none')
        base['operation'].fromScript('disjoint-over')

        for passName in passNames[1:]:
            node = sourceNodes[passName]
            lastMergeXpos = node.xpos()
            base = nuke.nodes.Merge2(xpos=lastMergeXpos, ypos=mergeYpos, inputs=[base, node])
            base['maskChannelMask'].fromScript('none')
            base['operation'].fromScript('disjoint-over')

        return self.styleTextLabel(nuke.nodes.Text2(xpos=lastMergeXpos + 200, ypos=mergeYpos, inputs=[base]), 'Slap Comp')

    def createOracleVarianceMap(self, xpos, ypos) -> nuke.Node:
        '''
        Builds the Oracle Variance Map section: shuffles each cg pass's cputime channel
        into rgb and merges them, wrapped in a contact sheet. 
        Returns the label node.
        '''
        passSpacing = 150
        passXpos = xpos
        oracleReads = {}

        for passName in CG_PASSES:
            readNode = self.loadFreshPassRead(passName, passXpos, ypos, raw=True)
            if not readNode:
                continue
            oracleReads[passName] = readNode
            passXpos += passSpacing

        if not oracleReads:
            return None

        shuffleYpos = ypos + 100
        shuffles = []
        for passName, readNode in oracleReads.items():
            shuffle = nuke.nodes.Shuffle2(xpos=readNode.xpos(), ypos=shuffleYpos, inputs=[readNode])

            cputimeChannels = sorted(channel for channel in readNode.channels() if channel.split('.')[0] == 'cputime')
            if cputimeChannels:
                sourceChannel = cputimeChannels[0]
                shuffle['in1'].fromScript('cputime')
                shuffle['mappings'].setValue([
                    (0, sourceChannel, 'rgba.red'),
                    (0, sourceChannel, 'rgba.green'),
                    (0, sourceChannel, 'rgba.blue'),
                    (-1, 'white', 'rgba.alpha'),
                ])
            
            shuffles.append(shuffle) # will leave the shuffle's default settings if no cputime channel is found

        mergeYpos = shuffleYpos + 100
        mergeXpos = self.centeredXpos(shuffles)
        merge = shuffles[0]
        for shuffle in shuffles[1:]:
            merge = nuke.nodes.Merge2(xpos=mergeXpos, ypos=mergeYpos, inputs=[merge, shuffle])
            merge['maskChannelMask'].fromScript('none')
            merge['operation'].fromScript('max')
        
        rawReadNode = next(iter(oracleReads.values()))
        statsText = (
            "[python {__import__('lightingDiagnostics').oracleStatsText(%r, nuke.frame())}]"
            % rawReadNode.fullName()
        )

        statsSource = merge
        if statsText:
            statsSource = self.styleTextLabel(
                nuke.nodes.Text2(xpos=mergeXpos, ypos=mergeYpos + 50, inputs=[merge]), statsText, xjustify='right'
            )

        statsSource['global_font_scale'].setValue(0.4)

        contactSheetLabel = self.styleTextLabel(
            nuke.nodes.Text2(xpos=mergeXpos, ypos=mergeYpos + 100, inputs=[statsSource]), 'Oracle Variance'
        )

        backdropNodes = list(oracleReads.values()) + shuffles + [merge]
        if statsSource is not merge:
            backdropNodes.append(statsSource)
        backdropNodes.append(contactSheetLabel)
        self.makeBackdrop(
            backdropNodes,
            '<center>oracle variance', tile_color=676813311, z_order=-2.0, note_font_size=80.0
        )

        return contactSheetLabel

    def createOutput(self, tiles, message, xpos, ypos) -> nuke.Node:
        '''
        Builds the final contact sheet combining every section's output tile (Slap Comp, Balls, LPEs, Channels),
        then appends a WriteTank node below it for viewing.
        '''
        label = self.buildContactSheet(
            tiles, message, xpos, ypos, roworder='TopBottom', colorder='LeftRight', titleLabel=True,
            width=PROJECT_FORMAT.width() * 2, height=PROJECT_FORMAT.height() * 2, gap=0.0
        )
        if not label:
            return None

        #node_WriteTank_1
        node_WriteTank_1 = nuke.nodes.WriteTank(xpos=label.xpos(), ypos=label.ypos() + 200, inputs=[label])
        node_WriteTank_1['cached'].setValue(True)
        node_WriteTank_1['tank_channel'].fromScript('output1')

        self.makeBackdrop([node_WriteTank_1], '<center>VIEW', tile_color=674847231)

        return label

    # ---------------------------------------------------------------------------- #
    #                           Section builder helpers                            #
    # ---------------------------------------------------------------------------- #
    def createPuzzleMatteTile(self, xpos, ypos, tileYpos) -> nuke.Node:
        '''
        Builds the Channels section's Puzzle Matte tile: fresh reads for each CG pass flagged
        puzzleMattes=True, each shuffled into its assigned PuzzleMatte_XX pair and merged together. 
        The pass without assigned PuzzleMatte channels but aren't actually rendered yet 
        (puzzleMattes=True in CG_PASSES only means it's planned, not that it's on disk) won't be built.
        Returns the labeled, cleaned tile, or None if no pass has puzzle mattes available.
        '''
        passSpacing = 260
        puzzleXpos = xpos
        puzzleReads = {}
        puzzleBackdrops = {}

        for passName, cfg in CG_PASSES.items():
            if not cfg['puzzleMattes']:
                continue
            readNode = self.loadFreshPassRead(passName, puzzleXpos, ypos)
            if not readNode:
                continue
            puzzleReads[passName] = readNode
            puzzleBackdrops[passName] = self.makeBackdrop(
                [readNode], '<center>' + passName, tile_color=READ_NODE_TILE_COLOR, z_order=-1.0, note_font_size=80.0, width=150.0, height=220.0
            )
            puzzleXpos += passSpacing

        if not puzzleReads:
            return None

        shuffleYpos = ypos + 130
        shuffleSpacing = 100
        shuffles = []
        keptBackdrops = []
        for index, (passName, readNode) in enumerate(puzzleReads.items()):
            namedMattes = (f'puzzle_{passName}_a', f'puzzle_{passName}_b')
            legacyMattes = (f'PuzzleMatte_{2*index + 1:02d}', f'PuzzleMatte_{2*index + 2:02d}')

            layers = set(channel.split('.')[0] for channel in readNode.channels())
            isNamed = any(matteName in layers for matteName in namedMattes)
            if isNamed:
                matteNames = namedMattes
            elif any(matteName in layers for matteName in legacyMattes):
                matteNames = legacyMattes
            else:
                nuke.warning(readNode.name() + ': no ' + '/'.join(namedMattes + legacyMattes) + ' channels found, puzzle mattes not built for ' + passName + '.')
                nuke.delete(puzzleBackdrops[passName])
                nuke.delete(readNode)
                continue

            keptBackdrops.append(puzzleBackdrops[passName])
            rowWidth = (len(matteNames) - 1) * shuffleSpacing
            shuffleXpos = readNode.xpos() - rowWidth / 2.0
            for suffix, matteName in zip('ab', matteNames):
                shuffle = nuke.nodes.Shuffle2(xpos=shuffleXpos, ypos=shuffleYpos, inputs=[readNode])
                shuffle['in1'].fromScript(matteName)

                cellLabel = matteName if isNamed else passName + '_' + suffix + ' - ' + matteName.replace('_', '')
                cellFormat = shuffle.format()
                topBox = (0.0, cellFormat.height() - 101.0, cellFormat.width(), cellFormat.height())
                labeledShuffle = self.styleTextLabel(
                    nuke.nodes.Text2(xpos=shuffleXpos, ypos=shuffleYpos + 24, inputs=[shuffle]), cellLabel, box=topBox, xjustify='right'
                )
                labeledShuffle['font_size'].setExpression('height * 0.1')
                labeledShuffle['global_font_scale'].setValue(2)
                shuffles.append(labeledShuffle)
                shuffleXpos += shuffleSpacing

        if not shuffles:
            return None

        sheetYpos = shuffleYpos + 300
        sheetXpos = self.centeredXpos(shuffles)
        columns = math.ceil(math.sqrt(len(shuffles)))
        rows = math.ceil(len(shuffles) / columns)
        contactSheet = nuke.nodes.ContactSheet(xpos=sheetXpos, ypos=sheetYpos, inputs=shuffles)
        contactSheet['width'].setValue(PROJECT_FORMAT.width() / 2)
        contactSheet['height'].setValue(PROJECT_FORMAT.height() / 2)
        contactSheet['rows'].setValue(rows)
        contactSheet['columns'].setValue(columns)
        contactSheet['gap'].setValue(0.0)
        contactSheet['center'].setValue(True)
        contactSheet['startframe'].setValue(1.0)
        contactSheet['endframe'].setValue(1.0)
        contactSheet['roworder'].fromScript('TopBottom')

        self.makeBackdrop(
            keptBackdrops + shuffles,
            '<center>puzzle matte', tile_color=676934143, z_order=-2.0, note_font_size=80.0
        )

        remove = nuke.nodes.Remove(xpos=sheetXpos, ypos=tileYpos, inputs=[contactSheet])
        remove['operation'].fromScript('keep')
        remove['channels'].fromScript('rgb')
        remove['channels2'].fromScript('alpha')
        return remove

    def createVFXChannelsRestPasses(self, xpos, ypos, tileYpos) -> list:
        '''
        Builds a tile for every VFX pass found on disk.
        Returns the tiles list.
        '''
        excludedPasses = set(CG_PASSES) | {'plate', 'macbethprobe'}
        restPasses = self.restPassOrder(name for name in self.reads if name not in excludedPasses)

        tiles = []
        passBackdrops = []
        tileNodes = {}
        tileXpos = xpos
        for passName in restPasses:
            readNode = self.reads[passName]
            readNode.setXYpos(tileXpos, ypos)

            displayName = passName.replace('_', ' ').title()
            backdropLabel = AUX_PASSES.get(passName, {}).get('label', '<center>' + displayName)
            passBackdrops.append(self.makeBackdrop(
                [readNode], backdropLabel, tile_color=READ_NODE_TILE_COLOR, z_order=-1.0, note_font_size=80.0, width=150.0, height=220.0
            ))

            customized = self.addChannelsCustomSetup(readNode, passName, tileXpos, ypos + 130)
            labelColor = [0, 0, 0, 1] if passName == 'ambient_occlusion' else None
            tile = self.labelAndCleanTile(customized, displayName, tileXpos, tileYpos, color=labelColor)
            tiles.append(tile)
            tileNodes[passName] = tile
            tileXpos += 300

        self.auxTileNodes = tileNodes

        if passBackdrops:
            self.makeBackdrop(
                passBackdrops,
                '<center>aux', tile_color=676813311, z_order=-2.0, note_font_size=80.0
            )

        return tiles

    def createCGTile(self, xpos, ypos, tileYpos) -> list:
        '''
        Builds the Passes section's cg reads.
        '''
        passSpacing = 260
        passXpos = xpos
        cgReads = {}
        passBackdrops = []

        for passName in CG_PASSES:
            readNode = self.loadFreshPassRead(passName, passXpos, ypos)
            if not readNode:
                continue
            cgReads[passName] = readNode
            passBackdrops.append(self.makeBackdrop(
                [readNode], '<center>' + passName, tile_color=READ_NODE_TILE_COLOR, z_order=-1.0, note_font_size=80.0, width=150.0, height=220.0
            ))
            passXpos += passSpacing

        self.cgReads = cgReads

        if not cgReads:
            return []

        self.makeBackdrop(
            passBackdrops,
            '<center>cg', tile_color=676813311, z_order=-2.0, note_font_size=80.0
        )

        tiles = []
        tileNodes = {}
        for name in CG_PASSES:
            if name not in cgReads:
                continue
            tile = self.labelAndCleanTile(cgReads[name], name.title(), cgReads[name].xpos(), tileYpos)
            tiles.append(tile)
            tileNodes[name] = tile
        self.cgTileNodes = tileNodes
        return tiles


# ---------------------------------------------------------------------------- #
#                                Button actions                                #
# ---------------------------------------------------------------------------- #
def _finishBuild(diag, tiles, message, outputX, outputY, existingNodes) -> LightingDiagnostics:
    diag.createOutput(tiles, message, outputX, outputY)
    _shiftBuildToAvoidOverlap(set(nuke.allNodes()) - existingNodes, existingNodes)
    return diag


def vfxLightingDiagnostics(version=None) -> LightingDiagnostics:
    existingNodes = set(nuke.allNodes())

    temp_dot = nuke.createNode('Dot')
    x, y = temp_dot.xpos(), temp_dot.ypos()
    nuke.delete(temp_dot)

    diag = LightingDiagnostics()
    if version:
        diag.loader.latest = version
    diag.loadPasses(x, y)

    # Positioning and section building.
    channelsTile = diag.createVFXPasses(x, y + 300)
    slapCompTile = diag.createVFXSlapComp(x, y - 1200)
    sectionGap = 400.0
    builtSoFar = set(nuke.allNodes()) - existingNodes
    sectionX = int(max((_nodeRightEdge(n) for n in builtSoFar), default=x) + sectionGap)
    beforeLPE = set(nuke.allNodes())
    lpeTile = diag.createLPEs(sectionX, y - 500)
    rawXpos = int(max((_nodeRightEdge(n) for n in set(nuke.allNodes()) - beforeLPE), default=sectionX) + sectionGap)
    diag.attachUsageLabels(diag.createRawUsageBlock(rawXpos, y - 500))
    ballsTile = diag.createBalls(sectionX + 600, y + 565)

    # Wrap all sections into a final contact sheet.
    builtSoFar = set(nuke.allNodes()) - existingNodes
    outputX = int(max((_nodeRightEdge(n) for n in builtSoFar), default=x) + sectionGap)
    outputY = y + 1020

    tiles = [tile for tile in [slapCompTile, ballsTile, lpeTile, channelsTile] if tile]

    return _finishBuild(diag, tiles, f'VFXLD {diag.loader.assetName}', outputX, outputY, existingNodes)


def cgLightingDiagnostics(version=None) -> LightingDiagnostics:
    existingNodes = set(nuke.allNodes())

    temp_dot = nuke.createNode('Dot')
    x, y = temp_dot.xpos(), temp_dot.ypos()
    nuke.delete(temp_dot)

    diag = LightingDiagnostics()
    if version:
        diag.loader.latest = version
    diag.loadPasses(x, y, passFilter=set(CG_PASSES))

    channelsTile = diag.createCGPasses(x, y + 300)
    slapCompTile = diag.createCGSlapComp()
    sectionGap = 400.0
    builtSoFar = set(nuke.allNodes()) - existingNodes
    sectionX = int(max((_nodeRightEdge(n) for n in builtSoFar), default=x) + sectionGap)
    beforeLPE = set(nuke.allNodes())
    lpeTile = diag.createLPEs(sectionX, y - 400)
    rawXpos = int(max((_nodeRightEdge(n) for n in set(nuke.allNodes()) - beforeLPE), default=sectionX) + sectionGap)
    diag.attachUsageLabels(diag.createRawUsageBlock(rawXpos, y - 400))
    oracleTile = diag.createOracleVarianceMap(sectionX, y + 600)

    builtSoFar = set(nuke.allNodes()) - existingNodes
    outputX = int(max((_nodeRightEdge(n) for n in builtSoFar), default=x) + sectionGap)
    outputY = y + 1100

    tiles = [tile for tile in [slapCompTile, oracleTile, lpeTile, channelsTile] if tile]
    return _finishBuild(diag, tiles, f'CGLD {diag.loader.assetName}', outputX, outputY, existingNodes)


# ---------------------------------------------------------------------------- #
#                                      UI                                      #
# ---------------------------------------------------------------------------- #
class LightingDiagnosticsUI(QtWidgets.QWidget):
    def __init__(self, parent=None):
        QtWidgets.QWidget.__init__(self, parent)
        self.loader = Loader()
        self.setWindowFlags(QtCore.Qt.WindowStaysOnTopHint)
        self.makeUI()

    def onLockVersionToggled(self, checked):
        '''
        When locked, greys out every version except the latest (index 0) so it can't be selected.
        '''
        model = self.versionMenu.model()
        for i in range(model.rowCount()):
            model.item(i).setEnabled(not checked or i == 0)
        if checked:
            self.versionMenu.setCurrentIndex(0)

    def onRefreshVersions(self):
        currentSelection = self.versionMenu.currentText()
        self.loader.refreshVersions()
        self.versionMenu.clear()
        self.versionMenu.addItems(self.loader.versions or [])
        if not self.lockVersionCheckbox.isChecked() and currentSelection in (self.loader.versions or []):
            self.versionMenu.setCurrentText(currentSelection)
        self.onLockVersionToggled(self.lockVersionCheckbox.isChecked())

    def makeUI(self):
        self.layout = QtWidgets.QVBoxLayout()
        self.setWindowTitle("Lighting Diagnostics")
        self.setObjectName("LightingDiagnosticsUI")
        self.setLayout(self.layout)

        self.versionMenu = QtWidgets.QComboBox()
        self.versionMenu.addItems(self.loader.versions or [])

        self.lockVersionCheckbox = QtWidgets.QCheckBox('Lock to Latest')
        self.lockVersionCheckbox.setChecked(True)
        self.lockVersionCheckbox.toggled.connect(self.onLockVersionToggled)

        self.refreshVersionsBtn = QtWidgets.QPushButton('Refresh')
        self.refreshVersionsBtn.clicked.connect(self.onRefreshVersions)

        versionRow = QtWidgets.QHBoxLayout()
        versionRow.addWidget(self.versionMenu)
        versionRow.addWidget(self.lockVersionCheckbox)
        versionRow.addWidget(self.refreshVersionsBtn)

        options = QtWidgets.QFormLayout()
        options.addRow(QtWidgets.QLabel('Lighting Versions: '), versionRow)
        self.layout.addLayout(options)

        self.onLockVersionToggled(self.lockVersionCheckbox.isChecked())

        self.updateLightingBtn = QtWidgets.QPushButton('Update Lighting to Selected Version')
        self.updateLightingBtn.clicked.connect(lambda: self.loader.updateLighting(self.versionMenu.currentText()))
        self.layout.addWidget(self.updateLightingBtn)

        self.vfxButton = QtWidgets.QPushButton('VFX Lighting Diagnostics')
        self.vfxButton.clicked.connect(lambda: vfxLightingDiagnostics(self.versionMenu.currentText()))
        self.layout.addWidget(self.vfxButton)

        self.cgButton = QtWidgets.QPushButton('CG Lighting Diagnostics')
        self.cgButton.clicked.connect(lambda: cgLightingDiagnostics(self.versionMenu.currentText()))
        self.layout.addWidget(self.cgButton)
        self.layout.setAlignment(QtCore.Qt.AlignTop)


def lightingDiagnosticsUI():
    nuke.LightingDiagnosticsUI = LightingDiagnosticsUI
    pane = nuke.getPaneFor('Properties.1')
    panels.registerWidgetAsPanel('nuke.LightingDiagnosticsUI', 'Lighting Diagnostics', 'ala.lightingDiagnosticsUI', True).addToPane(pane)
    try:
        nuke.root()['format'].setValue(PROJECT_FORMAT)
    except Exception as e:
        nuke.alert("Unable to set format to " + PROJECT_FORMAT.name() + "." + str(e))


def lightingDiagnostics():
    if sgtk.platform.current_engine().context.entity:
        try:
            lightingDiagnosticsUI()
        except IndexError:
            nuke.alert("Unable to open diagnostics. Check if your shot has renders available!")
        except Exception as e:
            print(e)
            nuke.alert("Unable to open diagnostics." + str(e))
    else:
        nuke.alert("Unable to open diagnostics. Make sure you're in a Shotgrid context first!")