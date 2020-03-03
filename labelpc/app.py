# -*- coding: utf-8 -*-

import functools
import os
import os.path as osp
import re
import webbrowser

import PIL.Image
from io import BytesIO
import numpy as np

import imgviz
from qtpy import QtCore
from qtpy.QtCore import Qt
from qtpy import QtGui
from qtpy import QtWidgets
from PyQt5.QtWidgets import QInputDialog, QDialog, QLineEdit, QDialogButtonBox, QFormLayout

from labelpc import __appname__
from labelpc import PY2
from labelpc import QT5

from labelpc.dialogs.open_file_dialog import OpenFileDialog

from . import utils
from labelpc.config import get_config
from labelpc.label_file import LabelFile
from labelpc.label_file import LabelFileError
from labelpc.logger import logger
from labelpc.shape import Shape
from labelpc.widgets import Canvas
from labelpc.widgets import ColorDialog
from labelpc.widgets import LabelDialog
from labelpc.widgets import LabelQListWidget
from labelpc.widgets import ToolBar
from labelpc.widgets import UniqueLabelQListWidget
from labelpc.widgets import ZoomWidget

from labelpc.pointcloud.PointCloud import PointCloud
from labelpc.pointcloud.Voxelize import VoxelGrid


# TODO:
#   Create annotations for individual slices ???
#   Snap to corner
#   Snap to center
#   Detect intersections of labels
#   Room alignment (user input rough align AND final, automatic fine alignment)
#   Cross hair targeting point annotations (cross hairs on beam points, toggle on/off?)
#   Add distance threshold for snap functions to config file
#   Interpolate beam positions
#   Break rack (turn one annotation into 2 and resize each independently) (manual mode AND automatic using beams)
#   Merge racks (turn two annotations into 1)
#   Rotate rack (change orientation {direction pallet goes into and out of rack})
#   Distinguish rack orientation in annotation
#   Create icons for buttons
#   Create shortcuts

LABEL_COLORMAP = imgviz.label_colormap(value=200)


class MainWindow(QtWidgets.QMainWindow):

    FIT_WINDOW, FIT_WIDTH, MANUAL_ZOOM = 0, 1, 2

    def __init__(
        self,
        config=None,
        filename=None,
        output=None,
        output_file=None,
        output_dir=None,
    ):

        if output is not None:
            logger.warning(
                'argument output is deprecated, use output_file instead'
            )
            if output_file is None:
                output_file = output

        # see labelpc/config/default_config.yaml for valid configuration
        if config is None:
            config = get_config()
        self._config = config

        super(MainWindow, self).__init__()
        self.setWindowTitle(__appname__)

        # Whether we need to save or not.
        self.dirty = False

        self._noSelectionSlot = False

        # Main widgets and related state.
        self.labelDialog = LabelDialog(
            parent=self,
            labels=self._config['labels'],
            sort_labels=self._config['sort_labels'],
            show_text_field=self._config['show_label_text_field'],
            completion=self._config['label_completion'],
            fit_to_content=self._config['fit_to_content'],
            flags=self._config['label_flags']
        )

        self.lastOpenDir = None

        self.flag_dock = self.flag_widget = None
        self.flag_dock = QtWidgets.QDockWidget(self.tr('Flags'), self)
        self.flag_dock.setObjectName('Flags')
        self.flag_widget = QtWidgets.QListWidget()
        if config['flags']:
            self.loadFlags({k: False for k in config['flags']})
        self.flag_dock.setWidget(self.flag_widget)
        self.flag_widget.itemChanged.connect(self.setDirty)

        self.labelList = LabelQListWidget()
        self.labelList.itemActivated.connect(self.labelSelectionChanged)
        self.labelList.itemSelectionChanged.connect(self.labelSelectionChanged)
        self.labelList.itemDoubleClicked.connect(self.editLabel)
        # Connect to itemChanged to detect checkbox changes.
        self.labelList.itemChanged.connect(self.labelItemChanged)
        self.labelList.setDragDropMode(
            QtWidgets.QAbstractItemView.InternalMove)
        self.labelList.setParent(self)
        self.shape_dock = QtWidgets.QDockWidget(
            self.tr('Polygon Labels'),
            self
        )
        self.shape_dock.setObjectName('Labels')
        self.shape_dock.setWidget(self.labelList)

        self.uniqLabelList = UniqueLabelQListWidget()
        self.uniqLabelList.itemActivated.connect(self.modeSelectionChanged)
        self.uniqLabelList.itemSelectionChanged.connect(self.modeSelectionChanged)
        self.uniqLabelList.setToolTip(self.tr(
            "Select label to start annotating for it. "
            "Press 'Esc' to deselect."))
        if self._config['labels']:
            for label in self._config['labels']:
                item = self.uniqLabelList.createItemFromLabel(label)
                self.uniqLabelList.addItem(item)
                rgb = self._get_rgb_by_label(label)
                self.uniqLabelList.setItemLabel(item, label, rgb)
        self.label_dock = QtWidgets.QDockWidget(self.tr(u'Label List'), self)
        self.label_dock.setObjectName(u'Label List')
        self.label_dock.setWidget(self.uniqLabelList)

        self.fileSearch = QtWidgets.QLineEdit()
        self.fileSearch.setPlaceholderText(self.tr('Search Filename'))
        self.fileSearch.textChanged.connect(self.fileSearchChanged)
        self.fileListWidget = QtWidgets.QListWidget()
        self.fileListWidget.itemSelectionChanged.connect(
            self.fileSelectionChanged
        )
        fileListLayout = QtWidgets.QVBoxLayout()
        fileListLayout.setContentsMargins(0, 0, 0, 0)
        fileListLayout.setSpacing(0)
        fileListLayout.addWidget(self.fileSearch)
        fileListLayout.addWidget(self.fileListWidget)
        self.file_dock = QtWidgets.QDockWidget(self.tr(u'File List'), self)
        self.file_dock.setObjectName(u'Files')
        fileListWidget = QtWidgets.QWidget()
        fileListWidget.setLayout(fileListLayout)
        self.file_dock.setWidget(fileListWidget)

        self.zoomWidget = ZoomWidget()
        self.colorDialog = ColorDialog(parent=self)

        self.canvas = self.labelList.canvas = Canvas(
            epsilon=self._config['epsilon'],
            double_click=self._config['canvas']['double_click'],
        )
        self.canvas.zoomRequest.connect(self.zoomRequest)

        scrollArea = QtWidgets.QScrollArea()
        scrollArea.setWidget(self.canvas)
        scrollArea.setWidgetResizable(True)
        self.scrollBars = {
            Qt.Vertical: scrollArea.verticalScrollBar(),
            Qt.Horizontal: scrollArea.horizontalScrollBar(),
        }
        self.canvas.scrollRequest.connect(self.scrollRequest)
        self.canvas.nextSliceRequest.connect(self.showNextSlice)
        self.canvas.lastSliceRequest.connect(self.showLastSlice)

        self.canvas.newShape.connect(self.newShape)
        self.canvas.shapeMoved.connect(self.setDirty)
        self.canvas.selectionChanged.connect(self.shapeSelectionChanged)
        self.canvas.drawingPolygon.connect(self.toggleDrawingSensitive)
        self.canvas.splitRack.connect(self.splitRack)

        self.setCentralWidget(scrollArea)

        features = QtWidgets.QDockWidget.DockWidgetFeatures()
        for dock in ['flag_dock', 'label_dock', 'shape_dock', 'file_dock']:
            if self._config[dock]['closable']:
                features = features | QtWidgets.QDockWidget.DockWidgetClosable
            if self._config[dock]['floatable']:
                features = features | QtWidgets.QDockWidget.DockWidgetFloatable
            if self._config[dock]['movable']:
                features = features | QtWidgets.QDockWidget.DockWidgetMovable
            getattr(self, dock).setFeatures(features)
            if self._config[dock]['show'] is False:
                getattr(self, dock).setVisible(False)

        self.addDockWidget(Qt.RightDockWidgetArea, self.flag_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.label_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.shape_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.file_dock)

        # Actions
        action = functools.partial(utils.newAction, self)
        shortcuts = self._config['shortcuts']
        quit = action(self.tr('&Quit'), self.close, shortcuts['quit'], 'quit',
                      self.tr('Quit application'))
        open_ = action(self.tr('&Open'),
                       self.openPointCloud,
                       shortcuts['open'],
                       'open',
                       self.tr('Open point cloud file'))
        opendir = action(self.tr('&Open Dir'), self.openDirDialog,
                         shortcuts['open_dir'], 'open', self.tr(u'Open Dir'))
        showNextSlice = action(
            self.tr('Next Slice'),
            self.showNextSlice,
            None,
            'next slice',
            self.tr(u'Show next slice of point cloud'),
            enabled=False,
        )
        showLastSlice = action(
            self.tr('Last Slice'),
            self.showLastSlice,
            None,
            'next slice',
            self.tr(u'Show previous slice of point cloud'),
            enabled=False,
        )
        save = action(self.tr('&Save'),
                      self.saveFile, shortcuts['save'], 'save',
                      self.tr('Save labels to file'), enabled=False)
        saveAs = action(self.tr('&Save As'), self.saveFileAs,
                        shortcuts['save_as'],
                        'save-as', self.tr('Save labels to a different file'),
                        enabled=False)

        deleteFile = action(
            self.tr('&Delete File'),
            self.deleteFile,
            shortcuts['delete_file'],
            'delete',
            self.tr('Delete current label file'),
            enabled=False)

        changeOutputDir = action(
            self.tr('&Change Output Dir'),
            slot=self.changeOutputDirDialog,
            shortcut=shortcuts['save_to'],
            icon='open',
            tip=self.tr(u'Change where annotations are loaded/saved')
        )

        saveAuto = action(
            text=self.tr('Save &Automatically'),
            slot=lambda x: self.actions.saveAuto.setChecked(x),
            icon='save',
            tip=self.tr('Save automatically'),
            checkable=True,
            enabled=True,
        )
        saveAuto.setChecked(self._config['auto_save'])

        saveWithImageData = action(
            text='Save With Image Data',
            slot=self.enableSaveImageWithData,
            tip='Save image data in label file',
            checkable=True,
            checked=False,
            #checked=self._config['store_data'],
        )

        close = action('&Close', self.closeFile, shortcuts['close'], 'close',
                       'Close current file')

        align_room = action('Align Room', self.alignRoom, None, 'align', 'Align the room using walls')

        render_3d = action('Render points in 3D', self.render3d, None, 'render', 'Render the points in 3D')

        highlight_walls = action('Highlight walls', self.highlightWalls, None, 'highlight', 'Highlight walls')

        view_annotation_3d = action('View Label 3D', self.viewAnnotation3d, None, 'view 3d', 'View annotation 3d')

        update_annotation = action('Update Label', self.updateSelectedLabelWithHighlightedPoints, None, 'update label',
                                   'Update the label based on the points currently highlighted in the 3d viewer')

        split_all_racks = action('Split All Racks', self.splitRacks, None, 'split racks near beams',
                                 'Split the racks that are broken up due to proximity to support beams')

        merge_racks = action('Merge Racks', self.unsplitRacks, None, 'merge selected racks',
                             'Merge the selected racks into a single rack (undo rack split).')

        toggle_keep_prev_mode = action(
            self.tr('Keep Previous Annotation'),
            self.toggleKeepPrevMode,
            shortcuts['toggle_keep_prev_mode'], None,
            self.tr('Toggle "keep pevious annotation" mode'),
            checkable=True)
        toggle_keep_prev_mode.setChecked(self._config['keep_prev'])

        createMode = action(
            self.tr('Create Polygons'),
            lambda: self.toggleDrawMode(False, createMode='polygon'),
            shortcuts['create_polygon'],
            'objects',
            self.tr('Start drawing polygons'),
            enabled=False,
        )
        createRectangleMode = action(
            self.tr('Create Rectangle'),
            lambda: self.toggleDrawMode(False, createMode='rectangle'),
            shortcuts['create_rectangle'],
            'objects',
            self.tr('Start drawing rectangles'),
            enabled=False,
        )
        createCircleMode = action(
            self.tr('Create Circle'),
            lambda: self.toggleDrawMode(False, createMode='circle'),
            shortcuts['create_circle'],
            'objects',
            self.tr('Start drawing circles'),
            enabled=False,
        )
        createLineMode = action(
            self.tr('Create Line'),
            lambda: self.toggleDrawMode(False, createMode='line'),
            shortcuts['create_line'],
            'objects',
            self.tr('Start drawing lines'),
            enabled=False,
        )
        createPointMode = action(
            self.tr('Create Point'),
            lambda: self.toggleDrawMode(False, createMode='point'),
            shortcuts['create_point'],
            'objects',
            self.tr('Start drawing points'),
            enabled=False,
        )
        createLineStripMode = action(
            self.tr('Create LineStrip'),
            lambda: self.toggleDrawMode(False, createMode='linestrip'),
            shortcuts['create_linestrip'],
            'objects',
            self.tr('Start drawing linestrip. Ctrl+LeftClick ends creation.'),
            enabled=False,
        )
        editMode = action(self.tr('Edit Polygons'), self.setEditMode,
                          shortcuts['edit_polygon'], 'edit',
                          self.tr('Move and edit the selected polygons'),
                          enabled=False)

        delete = action(self.tr('Delete Polygons'), self.deleteSelectedShape,
                        shortcuts['delete_polygon'], 'cancel',
                        self.tr('Delete the selected polygons'), enabled=False)
        copy = action(self.tr('Duplicate Polygons'), self.copySelectedShape,
                      shortcuts['duplicate_polygon'], 'copy',
                      self.tr('Create a duplicate of the selected polygons'),
                      enabled=False)
        undoLastPoint = action(self.tr('Undo last point'),
                               self.canvas.undoLastPoint,
                               shortcuts['undo_last_point'], 'undo',
                               self.tr('Undo last drawn point'), enabled=False)
        addPointToEdge = action(
            self.tr('Add Point to Edge'),
            self.canvas.addPointToEdge,
            None,
            'edit',
            self.tr('Add point to the nearest edge'),
            enabled=False,
        )
        removePoint = action(
            text='Remove Selected Point',
            slot=self.canvas.removeSelectedPoint,
            icon='edit',
            tip='Remove selected point from polygon',
            enabled=False,
        )

        undo = action(self.tr('Undo'), self.undoShapeEdit,
                      shortcuts['undo'], 'undo',
                      self.tr('Undo last add and edit of shape'),
                      enabled=False)

        hideAll = action(self.tr('&Hide\nPolygons'),
                         functools.partial(self.togglePolygons, False),
                         icon='eye', tip=self.tr('Hide all polygons'),
                         enabled=False)
        showAll = action(self.tr('&Show\nPolygons'),
                         functools.partial(self.togglePolygons, True),
                         icon='eye', tip=self.tr('Show all polygons'),
                         enabled=False)

        help = action(self.tr('&Tutorial'), self.tutorial, icon='help',
                      tip=self.tr('Show tutorial page'))

        zoom = QtWidgets.QWidgetAction(self)
        zoom.setDefaultWidget(self.zoomWidget)
        self.zoomWidget.setWhatsThis(
            self.tr(
                'Zoom in or out of the image. Also accessible with '
                '{} and {} from the canvas.'
            ).format(
                utils.fmtShortcut(
                    '{},{}'.format(
                        shortcuts['zoom_in'], shortcuts['zoom_out']
                    )
                ),
                utils.fmtShortcut(self.tr("Ctrl+Wheel")),
            )
        )
        self.zoomWidget.setEnabled(False)

        zoomIn = action(self.tr('Zoom &In'),
                        functools.partial(self.addZoom, 1.1),
                        shortcuts['zoom_in'], 'zoom-in',
                        self.tr('Increase zoom level'), enabled=False)
        zoomOut = action(self.tr('&Zoom Out'),
                         functools.partial(self.addZoom, 0.9),
                         shortcuts['zoom_out'], 'zoom-out',
                         self.tr('Decrease zoom level'), enabled=False)
        zoomOrg = action(self.tr('&Original size'),
                         functools.partial(self.setZoom, 100),
                         shortcuts['zoom_to_original'], 'zoom',
                         self.tr('Zoom to original size'), enabled=False)
        fitWindow = action(self.tr('&Fit Window'), self.setFitWindow,
                           shortcuts['fit_window'], 'fit-window',
                           self.tr('Zoom follows window size'), checkable=True,
                           enabled=False)
        fitWidth = action(self.tr('Fit &Width'), self.setFitWidth,
                          shortcuts['fit_width'], 'fit-width',
                          self.tr('Zoom follows window width'),
                          checkable=True, enabled=False)
        # Group zoom controls into a list for easier toggling.
        zoomActions = (self.zoomWidget, zoomIn, zoomOut, zoomOrg,
                       fitWindow, fitWidth)
        self.zoomMode = self.FIT_WINDOW
        fitWindow.setChecked(Qt.Checked)
        self.scalers = {
            self.FIT_WINDOW: self.scaleFitWindow,
            self.FIT_WIDTH: self.scaleFitWidth,
            # Set to one to scale to 100% when loading files.
            self.MANUAL_ZOOM: lambda: 1,
        }

        edit = action(self.tr('&Edit Label'), self.editLabel,
                      shortcuts['edit_label'], 'edit',
                      self.tr('Modify the label of the selected polygon'),
                      enabled=False)

        fill_drawing = action(
            self.tr('Fill Drawing Polygon'),
            self.canvas.setFillDrawing,
            None,
            'color',
            self.tr('Fill polygon while drawing'),
            checkable=True,
            enabled=True,
        )
        fill_drawing.trigger()

        # Label list context menu.
        labelMenu = QtWidgets.QMenu()
        utils.addActions(labelMenu, (edit, delete))
        self.labelList.setContextMenuPolicy(Qt.CustomContextMenu)
        self.labelList.customContextMenuRequested.connect(
            self.popLabelListMenu)

        # Store actions for further handling.
        self.actions = utils.struct(
            saveAuto=saveAuto,
            saveWithImageData=saveWithImageData,
            changeOutputDir=changeOutputDir,
            save=save, saveAs=saveAs, open=open_, close=close,
            deleteFile=deleteFile,
            toggleKeepPrevMode=toggle_keep_prev_mode,
            delete=delete, edit=edit, copy=copy,
            undoLastPoint=undoLastPoint, undo=undo,
            addPointToEdge=addPointToEdge, removePoint=removePoint,
            createMode=createMode, editMode=editMode,
            createRectangleMode=createRectangleMode,
            createCircleMode=createCircleMode,
            createLineMode=createLineMode,
            createPointMode=createPointMode,
            createLineStripMode=createLineStripMode,
            zoom=zoom, zoomIn=zoomIn, zoomOut=zoomOut, zoomOrg=zoomOrg,
            fitWindow=fitWindow, fitWidth=fitWidth,
            zoomActions=zoomActions,
            showNextSlice=showNextSlice, showLastSlice=showLastSlice,
            alignRoom=align_room,
            render3d=render_3d,
            highlightWalls=highlight_walls,
            viewAnnotation3d=view_annotation_3d,
            updateAnnotation=update_annotation,
            splitAllRacks=split_all_racks,
            mergeRacks=merge_racks,
            #fileMenuActions=(open_, opendir, save, saveAs, close, quit),
            fileMenuActions=(open_, save, saveAs, close, quit),
            tool=(),
            # XXX: need to add some actions here to activate the shortcut
            editMenu=(
                edit,
                copy,
                delete,
                None,
                undo,
                undoLastPoint,
                None,
                addPointToEdge,
                None,
                toggle_keep_prev_mode,
            ),
            # menu shown at right click
            menu=(
                createMode,
                createRectangleMode,
                createCircleMode,
                createLineMode,
                createPointMode,
                createLineStripMode,
                editMode,
                edit,
                copy,
                delete,
                undo,
                undoLastPoint,
                addPointToEdge,
                removePoint,
            ),
            onLoadActive=(
                close,
                showNextSlice,
                showLastSlice,
                align_room,
                render_3d,
                highlight_walls,
                update_annotation,
                split_all_racks,
                merge_racks,
                createMode,
                createRectangleMode,
                createCircleMode,
                createLineMode,
                createPointMode,
                createLineStripMode,
                editMode,
            ),
            onShapesPresent=(saveAs, hideAll, showAll),
        )

        self.canvas.edgeSelected.connect(self.canvasShapeEdgeSelected)
        self.canvas.vertexSelected.connect(self.actions.removePoint.setEnabled)

        self.menus = utils.struct(
            file=self.menu(self.tr('&File')),
            edit=self.menu(self.tr('&Edit')),
            view=self.menu(self.tr('&View')),
            help=self.menu(self.tr('&Help')),
            recentFiles=QtWidgets.QMenu(self.tr('Open &Recent')),
            labelList=labelMenu,
        )

        utils.addActions(
            self.menus.file,
            (
                open_,
                showNextSlice,
                showLastSlice,
                self.menus.recentFiles,
                save,
                saveAs,
                saveAuto,
                changeOutputDir,
                saveWithImageData,
                close,
                deleteFile,
                None,
                quit,
            ),
        )
        utils.addActions(self.menus.help, (help,))
        utils.addActions(
            self.menus.view,
            (
                self.flag_dock.toggleViewAction(),
                self.label_dock.toggleViewAction(),
                self.shape_dock.toggleViewAction(),
                self.file_dock.toggleViewAction(),
                None,
                fill_drawing,
                None,
                hideAll,
                showAll,
                render_3d,
                view_annotation_3d,
                None,
                zoomIn,
                zoomOut,
                zoomOrg,
                None,
                fitWindow,
                fitWidth,
                None,
            ),
        )

        self.menus.file.aboutToShow.connect(self.updateFileMenu)

        # Custom context menu for the canvas widget:
        utils.addActions(self.canvas.menus[0], self.actions.menu)
        utils.addActions(
            self.canvas.menus[1],
            (
                action('&Copy here', self.copyShape),
                action('&Move here', self.moveShape),
            ),
        )

        self.tools = self.toolbar('Tools')
        # Menu buttons on Left
        self.actions.tool = (
            open_,
            showNextSlice,
            showLastSlice,
            save,
            deleteFile,
            None,
            createMode,
            editMode,
            copy,
            delete,
            undo,
            None,
            zoomIn,
            zoom,
            zoomOut,
            fitWindow,
            fitWidth,
            align_room,
            render_3d,
            highlight_walls,
            update_annotation,
            split_all_racks,
            merge_racks,
        )

        self.statusBar().showMessage(self.tr('%s started.') % __appname__)
        self.statusBar().show()

        if output_file is not None and self._config['auto_save']:
            logger.warn(
                'If `auto_save` argument is True, `output_file` argument '
                'is ignored and output filename is automatically '
                'set as IMAGE_BASENAME.json.'
            )
        self.output_file = output_file
        self.output_dir = output_dir

        # Application state.
        self.image = QtGui.QImage()
        self.sourcePath = None
        self.recentFiles = []
        self.maxRecent = 7
        self.otherData = None
        self.zoom_level = 100
        self.fit_window = False
        self.max_points = None
        self.scale = None
        self.thickness = None
        self.offset = None
        self.annotationMode = None
        self.pointcloud = PointCloud(render=False)
        self.zoom_values = {}  # key=filename, value=(zoom_mode, zoom_value)
        self.scroll_values = {
            Qt.Horizontal: {},
            Qt.Vertical: {},
        }  # key=filename, value=scroll_value

        if filename is not None and osp.isdir(filename):
            self.importDirImages(filename, load=False)
        else:
            self.filename = filename

        if config['file_search']:
            self.fileSearch.setText(config['file_search'])
            self.fileSearchChanged()

        # XXX: Could be completely declarative.
        # Restore application settings.
        self.settings = QtCore.QSettings('labelpc', 'labelpc')
        # FIXME: QSettings.value can return None on PyQt4
        self.recentFiles = self.settings.value('recentFiles', []) or []
        size = self.settings.value('window/size', QtCore.QSize(600, 500))
        position = self.settings.value('window/position', QtCore.QPoint(0, 0))
        self.resize(size)
        self.move(position)
        # or simply:
        # self.restoreGeometry(settings['window/geometry']
        self.restoreState(
            self.settings.value('window/state', QtCore.QByteArray()))

        # Populate the File menu dynamically.
        self.updateFileMenu()
        # Since loading the file may take some time,
        # make sure it runs in the background.
        if self.filename is not None:
            self.queueEvent(functools.partial(self.loadFile, self.filename))

        # Callbacks:
        self.zoomWidget.valueChanged.connect(self.paintCanvas)

        self.populateModeActions()

        # self.firstStart = True
        # if self.firstStart:
        #    QWhatsThis.enterWhatsThisMode()

    def menu(self, title, actions=None):
        menu = self.menuBar().addMenu(title)
        if actions:
            utils.addActions(menu, actions)
        return menu

    def toolbar(self, title, actions=None):
        toolbar = ToolBar(title)
        toolbar.setObjectName('%sToolBar' % title)
        # toolbar.setOrientation(Qt.Vertical)
        toolbar.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        if actions:
            utils.addActions(toolbar, actions)
        self.addToolBar(Qt.LeftToolBarArea, toolbar)
        return toolbar

    # Support Functions

    def noShapes(self):
        return not self.labelList.itemsToShapes

    def populateModeActions(self):
        tool, menu = self.actions.tool, self.actions.menu
        self.tools.clear()
        utils.addActions(self.tools, tool)
        self.canvas.menus[0].clear()
        utils.addActions(self.canvas.menus[0], menu)
        self.menus.edit.clear()
        actions = (
            self.actions.createMode,
            self.actions.createRectangleMode,
            self.actions.createCircleMode,
            self.actions.createLineMode,
            self.actions.createPointMode,
            self.actions.createLineStripMode,
            self.actions.editMode,
        )
        utils.addActions(self.menus.edit, actions + self.actions.editMenu)

    def setDirty(self):
        if self._config['auto_save'] or self.actions.saveAuto.isChecked():
            label_file = osp.splitext(self.sourcePath)[0] + '.json'
            if self.output_dir:
                label_file_without_path = osp.basename(label_file)
                label_file = osp.join(self.output_dir, label_file_without_path)
            self.saveLabels(label_file)
            return
        self.dirty = True
        self.actions.save.setEnabled(True)
        self.actions.undo.setEnabled(self.canvas.isShapeRestorable)
        title = __appname__
        if self.filename is not None:
            title = '{} - {}*'.format(title, self.filename)
        self.setWindowTitle(title)

    def setClean(self):
        self.dirty = False
        self.actions.save.setEnabled(False)
        self.actions.createMode.setEnabled(True)
        self.actions.createRectangleMode.setEnabled(True)
        self.actions.createCircleMode.setEnabled(True)
        self.actions.createLineMode.setEnabled(True)
        self.actions.createPointMode.setEnabled(True)
        self.actions.createLineStripMode.setEnabled(True)
        title = __appname__
        if self.filename is not None:
            title = '{} - {}'.format(title, self.filename)
        self.setWindowTitle(title)

        if self.hasLabelFile():
            self.actions.deleteFile.setEnabled(True)
        else:
            self.actions.deleteFile.setEnabled(False)

    def toggleActions(self, value=True):
        """Enable/Disable widgets which depend on an opened image."""
        for z in self.actions.zoomActions:
            z.setEnabled(value)
        for action in self.actions.onLoadActive:
            action.setEnabled(value)

    def canvasShapeEdgeSelected(self, selected, shape):
        self.actions.addPointToEdge.setEnabled(
            selected and shape and shape.canAddPoint()
        )

    def queueEvent(self, function):
        QtCore.QTimer.singleShot(0, function)

    def status(self, message, delay=5000):
        self.statusBar().showMessage(message, delay)

    def resetState(self):
        self.image = QtGui.QImage()
        self.labelList.clear()
        self.sliceIdx = 0
        self.filename = None
        self.sourcePath = None
        self.imageData = None
        self.labelFile = None
        self.otherData = None
        self.max_points = None
        self.thickness = None
        self.scale = None
        self.offset = None
        self.annotationMode = None
        self.pointcloud.close_viewer()
        self.pointcloud = PointCloud(render=False)
        self.canvas.resetState()

    def currentItem(self):
        items = self.labelList.selectedItems()
        if items:
            return items[0]
        return None

    def addRecentFile(self, filename):
        if filename in self.recentFiles:
            self.recentFiles.remove(filename)
        elif len(self.recentFiles) >= self.maxRecent:
            self.recentFiles.pop()
        self.recentFiles.insert(0, filename)

    # Callbacks

    def undoShapeEdit(self):
        self.canvas.restoreShape()
        self.labelList.clear()
        self.loadShapes(self.canvas.shapes)
        self.actions.undo.setEnabled(self.canvas.isShapeRestorable)

    def tutorial(self):
        url = 'https://github.com/wkentaro/labelme/tree/master/examples/tutorial'  # NOQA
        webbrowser.open(url)

    def toggleDrawingSensitive(self, drawing=True):
        """Toggle drawing sensitive.

        In the middle of drawing, toggling between modes should be disabled.
        """
        self.actions.editMode.setEnabled(not drawing)
        self.actions.undoLastPoint.setEnabled(drawing)
        self.actions.undo.setEnabled(not drawing)
        self.actions.delete.setEnabled(not drawing)

    def toggleDrawMode(self, edit=True, createMode='polygon'):
        self.canvas.setEditing(edit)
        self.canvas.createMode = createMode
        if edit:
            self.actions.createMode.setEnabled(True)
            self.actions.createRectangleMode.setEnabled(True)
            self.actions.createCircleMode.setEnabled(True)
            self.actions.createLineMode.setEnabled(True)
            self.actions.createPointMode.setEnabled(True)
            self.actions.createLineStripMode.setEnabled(True)
        else:
            if createMode == 'polygon':
                self.actions.createMode.setEnabled(False)
                self.actions.createRectangleMode.setEnabled(True)
                self.actions.createCircleMode.setEnabled(True)
                self.actions.createLineMode.setEnabled(True)
                self.actions.createPointMode.setEnabled(True)
                self.actions.createLineStripMode.setEnabled(True)
            elif createMode == 'rectangle':
                self.actions.createMode.setEnabled(True)
                self.actions.createRectangleMode.setEnabled(False)
                self.actions.createCircleMode.setEnabled(True)
                self.actions.createLineMode.setEnabled(True)
                self.actions.createPointMode.setEnabled(True)
                self.actions.createLineStripMode.setEnabled(True)
            elif createMode == 'line':
                self.actions.createMode.setEnabled(True)
                self.actions.createRectangleMode.setEnabled(True)
                self.actions.createCircleMode.setEnabled(True)
                self.actions.createLineMode.setEnabled(False)
                self.actions.createPointMode.setEnabled(True)
                self.actions.createLineStripMode.setEnabled(True)
            elif createMode == 'point':
                self.actions.createMode.setEnabled(True)
                self.actions.createRectangleMode.setEnabled(True)
                self.actions.createCircleMode.setEnabled(True)
                self.actions.createLineMode.setEnabled(True)
                self.actions.createPointMode.setEnabled(False)
                self.actions.createLineStripMode.setEnabled(True)
            elif createMode == "circle":
                self.actions.createMode.setEnabled(True)
                self.actions.createRectangleMode.setEnabled(True)
                self.actions.createCircleMode.setEnabled(False)
                self.actions.createLineMode.setEnabled(True)
                self.actions.createPointMode.setEnabled(True)
                self.actions.createLineStripMode.setEnabled(True)
            elif createMode == "linestrip":
                self.actions.createMode.setEnabled(True)
                self.actions.createRectangleMode.setEnabled(True)
                self.actions.createCircleMode.setEnabled(True)
                self.actions.createLineMode.setEnabled(True)
                self.actions.createPointMode.setEnabled(True)
                self.actions.createLineStripMode.setEnabled(False)
            else:
                raise ValueError('Unsupported createMode: %s' % createMode)
        self.actions.editMode.setEnabled(not edit)

    def setEditMode(self):
        self.toggleDrawMode(True)

    def updateFileMenu(self):
        current = self.filename

        def exists(filename):
            return osp.exists(str(filename))

        menu = self.menus.recentFiles
        menu.clear()
        files = [f for f in self.recentFiles if f != current and exists(f)]
        for i, f in enumerate(files):
            icon = utils.newIcon('labels')
            action = QtWidgets.QAction(
                icon, '&%d %s' % (i + 1, QtCore.QFileInfo(f).fileName()), self)
            action.triggered.connect(functools.partial(self.loadRecent, f))
            menu.addAction(action)

    def popLabelListMenu(self, point):
        self.menus.labelList.exec_(self.labelList.mapToGlobal(point))

    def validateLabel(self, label):
        # no validation
        if self._config['validate_label'] is None:
            return True

        for i in range(self.uniqLabelList.count()):
            label_i = self.uniqLabelList.item(i).data(Qt.UserRole)
            if self._config['validate_label'] in ['exact']:
                if label_i == label:
                    return True
        return False

    def editLabel(self, item=False):
        if item and not isinstance(item, QtWidgets.QListWidgetItem):
            raise TypeError('unsupported type of item: {}'.format(type(item)))

        if not self.canvas.editing():
            return
        if not item:
            item = self.currentItem()
        if item is None:
            return
        shape = self.labelList.get_shape_from_item(item)
        if shape is None:
            return
        text, flags, group_id = self.labelDialog.popUp(
            text=shape.label, flags=shape.flags, group_id=shape.group_id,
        )
        if text is None:
            return
        if not self.validateLabel(text):
            self.errorMessage(
                self.tr('Invalid label'),
                self.tr(
                    "Invalid label '{}' with validation type '{}'"
                ).format(text, self._config['validate_label'])
            )
            return
        shape.label = text
        shape.flags = flags
        shape.group_id = group_id
        if shape.group_id is None:
            item.setText(shape.label)
        else:
            item.setText('{} ({})'.format(shape.label, shape.group_id))
        self.setDirty()
        if not self.uniqLabelList.findItemsByLabel(shape.label):
            item = QtWidgets.QListWidgetItem()
            item.setData(role=Qt.UserRole, value=shape.label)
            self.uniqLabelList.addItem(item)

    def modeSelectionChanged(self):
        items = self.uniqLabelList.selectedItems()
        if not items:
            self._config['display_label_popup'] = True
            self.annotationMode = None
            return
        label = items[0].data(Qt.UserRole)
        if label not in ['beam', 'select_rack', 'drive_in_rack', 'extra_deep_rack', 'pole', 'door', 'walls', 'noise']:
            self._config['display_label_popup'] = True
            self.annotationMode = None
            return

        self._config['display_label_popup'] = False
        self.annotationMode = label
        if label in ['beam', 'pole']:
            self.toggleDrawMode(False, createMode='point')
        elif 'rack' in label:
            self.toggleDrawMode(False, createMode='rectangle')
        elif label == 'door':
            self.toggleDrawMode(False, createMode='line')
            self._config['display_label_popup'] = True
        elif label in ['walls', 'noise']:
            self.toggleDrawMode(False, createMode='polygon')

    def fileSearchChanged(self):
        self.importDirImages(
            self.lastOpenDir,
            pattern=self.fileSearch.text(),
            load=False,
        )

    def fileSelectionChanged(self):
        items = self.fileListWidget.selectedItems()
        if not items:
            return
        item = items[0]

        if not self.mayContinue():
            return

        currIndex = self.imageList.index(str(item.text()))
        if currIndex < len(self.imageList):
            filename = self.imageList[currIndex]
            if filename:
                self.loadFile(filename)

    # React to canvas signals.
    def shapeSelectionChanged(self, selected_shapes):
        self._noSelectionSlot = True
        for shape in self.canvas.selectedShapes:
            shape.selected = False
        self.labelList.clearSelection()
        self.canvas.selectedShapes = selected_shapes
        for shape in self.canvas.selectedShapes:
            shape.selected = True
            item = self.labelList.get_item_from_shape(shape)
            item.setSelected(True)
            self.labelList.scrollToItem(item)
        self._noSelectionSlot = False
        n_selected = len(selected_shapes)
        self.actions.delete.setEnabled(n_selected)
        self.actions.copy.setEnabled(n_selected)
        self.actions.edit.setEnabled(n_selected == 1)
        if n_selected == 1 and self.pointcloud.viewer_is_ready():
            self.highlightPointsInLabel(self.canvas.selectedShapes[0])

    def addLabel(self, shape):
        if shape.group_id is None:
            text = shape.label
        else:
            text = '{} ({})'.format(shape.label, shape.group_id)
        item = QtWidgets.QListWidgetItem()
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked)
        self.labelList.itemsToShapes.append((item, shape))
        self.labelList.addItem(item)
        qlabel = QtWidgets.QLabel()
        qlabel.setText(text)
        qlabel.setAlignment(QtCore.Qt.AlignBottom)
        item.setSizeHint(qlabel.sizeHint())
        self.labelList.setItemWidget(item, qlabel)
        if not self.uniqLabelList.findItemsByLabel(shape.label):
            item = self.uniqLabelList.createItemFromLabel(shape.label)
            self.uniqLabelList.addItem(item)
            rgb = self._get_rgb_by_label(shape.label)
            self.uniqLabelList.setItemLabel(item, shape.label, rgb)
        self.labelDialog.addLabelHistory(shape.label)
        for action in self.actions.onShapesPresent:
            action.setEnabled(True)

        rgb = self._get_rgb_by_label(shape.label)
        if rgb is None:
            return

        r, g, b = rgb
        qlabel.setText(
            '{} <font color="#{:02x}{:02x}{:02x}">●</font>'
            .format(text, r, g, b)
        )
        shape.line_color = QtGui.QColor(r, g, b)
        shape.vertex_fill_color = QtGui.QColor(r, g, b)
        shape.hvertex_fill_color = QtGui.QColor(255, 255, 255)
        #shape.fill_color = QtGui.QColor(r, g, b, 128)
        shape.fill_color = QtGui.QColor(r, g, b, 64)
        shape.select_line_color = QtGui.QColor(255, 255, 255)
        #shape.select_fill_color = QtGui.QColor(r, g, b, 155)
        shape.select_fill_color = QtGui.QColor(r, g, b, 128)

    def _get_rgb_by_label(self, label):
        if self._config['shape_color'] == 'auto':
            item = self.uniqLabelList.findItemsByLabel(label)[0]
            label_id = self.uniqLabelList.indexFromItem(item).row() + 1
            label_id += self._config['shift_auto_shape_color']
            return LABEL_COLORMAP[label_id % len(LABEL_COLORMAP)]
        elif (self._config['shape_color'] == 'manual' and
              self._config['label_colors'] and
              label in self._config['label_colors']):
            return self._config['label_colors'][label]
        elif self._config['default_shape_color']:
            return self._config['default_shape_color']

    def remLabels(self, shapes):
        for shape in shapes:
            item = self.labelList.get_item_from_shape(shape)
            self.labelList.takeItem(self.labelList.row(item))

    def loadShapes(self, shapes, replace=True):
        self._noSelectionSlot = True
        for shape in shapes:
            self.addLabel(shape)
        self.labelList.clearSelection()
        self._noSelectionSlot = False
        self.canvas.loadShapes(shapes, replace=replace)

    def loadLabels(self, shapes):
        s = []
        for shape in shapes:
            label = shape['label']
            points = shape['points']
            shape_type = shape['shape_type']
            flags = shape['flags']
            group_id = shape.get('group_id')

            shape = Shape(
                label=label,
                shape_type=shape_type,
                group_id=group_id,
            )
            for p in points:
                shape.addPoint(self.pointcloudToQpoint(p))
            shape.close()

            default_flags = {}
            if self._config['label_flags']:
                for pattern, keys in self._config['label_flags'].items():
                    if re.match(pattern, label):
                        for key in keys:
                            default_flags[key] = False
            shape.flags = default_flags
            shape.flags.update(flags)

            s.append(shape)
        self.loadShapes(s)

    def loadFlags(self, flags):
        self.flag_widget.clear()
        for key, flag in flags.items():
            item = QtWidgets.QListWidgetItem(key)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if flag else Qt.Unchecked)
            self.flag_widget.addItem(item)

    def saveLabels(self, filename):
        lf = LabelFile()

        def format_shape(s):
            return dict(
                label=s.label.encode('utf-8') if PY2 else s.label,
                points=[self.qpointToPointcloud(p) for p in s.points],
                group_id=s.group_id,
                shape_type=s.shape_type,
                flags=s.flags
            )

        shapes = [format_shape(shape) for shape in self.labelList.shapes]
        flags = {}
        for i in range(self.flag_widget.count()):
            item = self.flag_widget.item(i)
            key = item.text()
            flag = item.checkState() == Qt.Checked
            flags[key] = flag
        try:
            sourcePath = osp.relpath(
                self.sourcePath, osp.dirname(filename))
            if osp.dirname(filename) and not osp.exists(osp.dirname(filename)):
                os.makedirs(osp.dirname(filename))
            lf.save(
                filename=filename,
                shapes=shapes,
                sourcePath=sourcePath,
                otherData=self.otherData,
                flags=flags,
            )
            self.labelFile = lf
            items = self.fileListWidget.findItems(
                self.sourcePath, Qt.MatchExactly
            )
            if len(items) > 0:
                if len(items) != 1:
                    raise RuntimeError('There are duplicate files.')
                items[0].setCheckState(Qt.Checked)
            # disable allows next and previous image to proceed
            # self.filename = filename
            return True
        except LabelFileError as e:
            self.errorMessage(
                self.tr('Error saving label data'),
                self.tr('<b>%s</b>') % e
            )
            return False

    def copySelectedShape(self):
        added_shapes = self.canvas.copySelectedShapes()
        self.labelList.clearSelection()
        for shape in added_shapes:
            self.addLabel(shape)
        self.setDirty()

    def labelSelectionChanged(self):
        if self._noSelectionSlot:
            return
        if self.canvas.editing():
            selected_shapes = []
            for item in self.labelList.selectedItems():
                shape = self.labelList.get_shape_from_item(item)
                selected_shapes.append(shape)
            if selected_shapes:
                self.canvas.selectShapes(selected_shapes)
            else:
                self.canvas.deSelectShape()

    def labelItemChanged(self, item):
        shape = self.labelList.get_shape_from_item(item)
        self.canvas.setShapeVisible(shape, item.checkState() == Qt.Checked)

    # Callback functions:

    def viewAnnotation3d(self):
        items = self.labelList.selectedItems()
        if items:
            points = self.labelList.get_shape_from_item(items[0]).points
            transformed = []
            for p in points:
                transformed.append(self.qpointToPointcloud(p))
            lookat = np.average(transformed, axis=0)
            self.viewLocation3d(lookat)

    def viewLocation3d(self, location):
        if not self.pointcloud.viewer_is_ready():
            self.render3d()
        if len(location) < 3:
            location = np.array((location[0], location[1], 3.0))
        self.pointcloud.viewer.set(lookat=location, theta=np.pi/4., r=15.0, phi=-np.pi/2.)

    def newShape(self):
        """Pop-up and give focus to the label editor.

        position MUST be in global coordinates.
        """
        # Get the label name from the uniqLabelList selected items
        items = self.uniqLabelList.selectedItems()
        text = None
        if items:
            text = items[0].data(Qt.UserRole)
        flags = {}
        group_id = None

        # Get label name and group id from user in popup window
        if self._config['display_label_popup'] or not text:
            previous_text = self.labelDialog.edit.text()
            text, flags, group_id = self.labelDialog.popUp(text)
            if not text:
                self.labelDialog.edit.setText(previous_text)

        if text and not self.validateLabel(text):
            self.errorMessage(
                self.tr('Invalid label'),
                self.tr(
                    "Invalid label '{}' with validation type '{}'"
                ).format(text, self._config['validate_label'])
            )
            text = ''
        if text:
            self.labelList.clearSelection()
            shape = self.canvas.setLastLabel(text, flags)
            shape.group_id = group_id
            # If this is a new pole or beam, snap the annotation to the center of the object
            if text == 'beam':
                intersection, intersected = self.nearestCrosshairIntersection(shape.points[0])
                if intersected:
                    print('Snapping to pole intersection')
                    shape.points[0] = self.pointcloudToQpoint(intersection)
                else:
                    transformed = self.qpointToPointcloud(shape.points[0])
                    snapped = self.pointcloud.snap_to_center(transformed, 0.5)
                    shape.points[0] = self.pointcloudToQpoint(snapped)
                if self.pointcloud.viewer_is_ready():
                    self.viewLocation3d(self.qpointToPointcloud(shape.points[0]))
            if text == 'pole':
                transformed = self.qpointToPointcloud(shape.points[0])
                snapped = self.pointcloud.snap_to_center(transformed, 0.5)
                shape.points[0] = self.pointcloudToQpoint(snapped)
            # If this is a new wall, snap the points to the corners of the walls
            elif text in ['wall', 'walls']:
                for i, p in enumerate(shape.points):
                    transformed = self.qpointToPointcloud(p)
                    snapped = self.pointcloud.snap_to_corner(transformed, 0.5)
                    shape.points[i] = self.pointcloudToQpoint(snapped)
            # If this is a new rack, split the rack into two racks if necessary and tighten box(es) to rack
            elif 'rack' in text:
                box = np.array([self.qpointToPointcloud(shape.points[0]), self.qpointToPointcloud(shape.points[1])])
                inbox = self.pointcloud.in_box_2d(box)
                if not np.sum(inbox):
                    print('No points selected')
                    return
                box = self.pointcloud.tighten_to_rack(box)
                inbox = self.pointcloud.in_box_2d(box)
                if self.isTwoRacks(text, box):
                    box, box2 = self.splitTwoRacks(text, box)
                    box, box2 = self.pointcloud.tighten_to_rack(box), self.pointcloud.tighten_to_rack(box2)
                    inbox, inbox2 = self.pointcloud.in_box_2d(box), self.pointcloud.in_box_2d(box2)
                    inbox[inbox2] = True
                    shape2 = shape.copy()
                    shape2.points[0], shape2.points[1] = self.pointcloudToQpoint(box2[0]), self.pointcloudToQpoint(box2[1])
                    self.addLabel(shape2)
                shape.points[0], shape.points[1] = self.pointcloudToQpoint(box[0]), self.pointcloudToQpoint(box[1])
                if self.pointcloud.viewer_is_ready():
                    self.pointcloud.highlight(self.pointcloud.select(inbox, highlighted=False))
            self.addLabel(shape)
            self.updatePixmap()
            self.actions.editMode.setEnabled(True)
            self.actions.undoLastPoint.setEnabled(False)
            self.actions.undo.setEnabled(True)
            self.setDirty()
        else:
            self.canvas.undoLastLine()
            self.canvas.shapesBackups.pop()

    def scrollRequest(self, delta, orientation):
        units = - delta * 0.1  # natural scroll
        bar = self.scrollBars[orientation]
        value = bar.value() + bar.singleStep() * units
        self.setScroll(orientation, value)

    def setScroll(self, orientation, value):
        self.scrollBars[orientation].setValue(value)
        self.scroll_values[orientation][self.filename] = value

    def setZoom(self, value):
        self.actions.fitWidth.setChecked(False)
        self.actions.fitWindow.setChecked(False)
        self.zoomMode = self.MANUAL_ZOOM
        self.zoomWidget.setValue(value)
        self.zoom_values[self.filename] = (self.zoomMode, value)

    def addZoom(self, increment=1.1):
        self.setZoom(self.zoomWidget.value() * increment)

    def zoomRequest(self, delta, pos):
        canvas_width_old = self.canvas.width()
        units = 1.1
        if delta < 0:
            units = 0.9
        self.addZoom(units)

        canvas_width_new = self.canvas.width()
        if canvas_width_old != canvas_width_new:
            canvas_scale_factor = canvas_width_new / canvas_width_old

            x_shift = round(pos.x() * canvas_scale_factor) - pos.x()
            y_shift = round(pos.y() * canvas_scale_factor) - pos.y()

            self.setScroll(
                Qt.Horizontal,
                self.scrollBars[Qt.Horizontal].value() + x_shift,
            )
            self.setScroll(
                Qt.Vertical,
                self.scrollBars[Qt.Vertical].value() + y_shift,
            )

    def setFitWindow(self, value=True):
        if value:
            self.actions.fitWidth.setChecked(False)
        self.zoomMode = self.FIT_WINDOW if value else self.MANUAL_ZOOM
        self.adjustScale()

    def setFitWidth(self, value=True):
        if value:
            self.actions.fitWindow.setChecked(False)
        self.zoomMode = self.FIT_WIDTH if value else self.MANUAL_ZOOM
        self.adjustScale()

    def togglePolygons(self, value):
        for item, shape in self.labelList.itemsToShapes:
            item.setCheckState(Qt.Checked if value else Qt.Unchecked)

    def loadFile(self, filename):
        # changing fileListWidget loads file
        if filename in self.imageList and self.fileListWidget.currentRow() != self.imageList.index(filename):
            self.fileListWidget.setCurrentRow(self.imageList.index(filename))
            self.fileListWidget.repaint()
            return

        self.canvas.setEnabled(False)
        self.resetState()
        dialog = OpenFileDialog()
        if dialog.exec():
            self.max_points, self.scale, self.thickness = dialog.getInputs()
        self.lastOpenDir = osp.dirname(filename)
        self.status(self.tr('Loading points from file'))
        self.loadPointCloud(filename)
        self.status(self.tr('Building voxel grid'))
        self.buildVoxelGrid()
        self.status(self.tr('Building pixel maps'))
        self.buildImageData()
        self.updatePixmap()
        self.loadLabelsFile(filename)
        self.setZoomAndScroll()
        self.canvas.setEnabled(True)

        self.paintCanvas()
        self.addRecentFile(self.filename)
        self.toggleActions(True)
        self.status(self.tr("Loaded %s") % osp.basename(str(filename)))

    def setZoomAndScroll(self):
        is_initial_load = not self.zoom_values
        if self.filename in self.zoom_values:
            self.zoomMode = self.zoom_values[self.filename][0]
            self.setZoom(self.zoom_values[self.filename][1])
        elif is_initial_load or not self._config['keep_prev_scale']:
            self.adjustScale(initial=True)
        # set scroll values
        for orientation in self.scroll_values:
            if self.filename in self.scroll_values[orientation]:
                self.setScroll(
                    orientation, self.scroll_values[orientation][self.filename]
                )

    def loadPointCloud(self, filename):
        filename = str(filename)
        if not QtCore.QFile.exists(filename):
            self.errorMessage(
                self.tr('Error opening file'),
                self.tr('No such file: <b>%s</b>') % filename
            )
            return False
        self.filename = str(filename)
        self.pointcloud.load(filename, self.max_points)
        self.status(self.tr("Loaded %s") % osp.basename(filename))

    def buildVoxelGrid(self):
        self.voxelgrid = VoxelGrid(self.pointcloud.points.loc[self.pointcloud.showing.bools][['x', 'y', 'z']].values,
                                   (self.scale, self.scale, self.thickness))
        offx, offy = self.voxelgrid.min_corner()[:2]
        self.offset = QtCore.QPointF(offx, offy)

    def buildImageData(self, scores=None, axis=2):
        if self.voxelgrid is None:
            logger.warn('No voxel grid built to make images from')
            return
        bitmaps = self.voxelgrid.bitmap2d(max=255, axis=axis, scores=scores)
        self.imageData = []
        # Create images from numpy arrays
        for m in bitmaps:
            img = PIL.Image.fromarray(np.asarray(m, dtype="uint8"))
            buff = BytesIO()
            img.save(buff, format="JPEG")
            buff.seek(0)
            self.imageData.append(buff.read())

    def updatePixmap(self):
        if not self.imageData:
            return
        if self.sliceIdx >= len(self.imageData):
            self.sliceIdx = 0
        if self.sliceIdx < 0:
            self.sliceIdx = len(self.imageData) - 1
        self.image = QtGui.QImage.fromData(self.imageData[self.sliceIdx])
        self.canvas.loadPixmap(QtGui.QPixmap.fromImage(self.image))
        self.canvas.loadShapes(self.labelList.shapes)

    def loadLabelsFile(self, filename):
        self.status(self.tr("Loading %s...") % osp.basename(str(filename)))
        label_file = osp.splitext(filename)[0] + '.json'
        if self.output_dir:
            label_file_without_path = osp.basename(label_file)
            label_file = osp.join(self.output_dir, label_file_without_path)
        if QtCore.QFile.exists(label_file) and \
                LabelFile.is_label_file(label_file):
            try:
                self.labelFile = LabelFile(label_file)
            except LabelFileError as e:
                self.errorMessage(
                    self.tr('Error opening file'),
                    self.tr(
                        "<p><b>%s</b></p>"
                        "<p>Make sure <i>%s</i> is a valid label file."
                    ) % (e, label_file)
                )
                self.status(self.tr("Error reading %s") % label_file)
                return False
            self.otherData = self.labelFile.otherData
        else:
            self.labelFile = None

        if self._config['keep_prev']:
            prev_shapes = self.canvas.shapes
        if self._config['flags']:
            self.loadFlags({k: False for k in self._config['flags']})
        if self.labelFile:
            self.loadLabels(self.labelFile.shapes)
            if self.labelFile.flags is not None:
                self.loadFlags(self.labelFile.flags)
        if self._config['keep_prev'] and not self.labelList.shapes:
            self.loadShapes(prev_shapes, replace=False)
            self.setDirty()
        else:
            self.setClean()

    def resizeEvent(self, event):
        if self.canvas and not self.image.isNull()\
           and self.zoomMode != self.MANUAL_ZOOM:
            self.adjustScale()
        super(MainWindow, self).resizeEvent(event)

    def paintCanvas(self):
        assert not self.image.isNull(), "cannot paint null image"
        self.canvas.scale = 0.01 * self.zoomWidget.value()
        self.canvas.adjustSize()
        self.canvas.update()

    def adjustScale(self, initial=False):
        value = self.scalers[self.FIT_WINDOW if initial else self.zoomMode]()
        value = int(100 * value)
        self.zoomWidget.setValue(value)
        self.zoom_values[self.filename] = (self.zoomMode, value)

    def scaleFitWindow(self):
        """Figure out the size of the pixmap to fit the main widget."""
        e = 2.0  # So that no scrollbars are generated.
        w1 = self.centralWidget().width() - e
        h1 = self.centralWidget().height() - e
        a1 = w1 / h1
        # Calculate a new scale value based on the pixmap's aspect ratio.
        w2 = self.canvas.pixmap.width() - 0.0
        h2 = self.canvas.pixmap.height() - 0.0
        a2 = w2 / h2
        return w1 / w2 if a2 >= a1 else h1 / h2

    def scaleFitWidth(self):
        # The epsilon does not seem to work too well here.
        w = self.centralWidget().width() - 2.0
        return w / self.canvas.pixmap.width()

    def enableSaveImageWithData(self, enabled):
        self._config['store_data'] = enabled
        self.actions.saveWithImageData.setChecked(enabled)

    def closeEvent(self, event):
        if not self.mayContinue():
            event.ignore()
        self.settings.setValue(
            'filename', self.filename if self.filename else '')
        self.settings.setValue('window/size', self.size())
        self.settings.setValue('window/position', self.pos())
        self.settings.setValue('window/state', self.saveState())
        self.settings.setValue('recentFiles', self.recentFiles)
        # ask the use for where to save the labels
        # self.settings.setValue('window/geometry', self.saveGeometry())

    def nearestCrosshairIntersection(self, point, threshold=0.3):
        beams = []
        for item, shape in self.labelList.itemsToShapes:
            if shape.label == 'beam':
                beams.append(self.qpointToPointcloud(shape.points[0]))
        px, py = self.qpointToPointcloud(point)
        intersection = np.array((px, py))
        intersected = False
        for x, y in beams:
            if abs(x - px) < threshold:
                intersection[0] = x
                intersected = True
            if abs(y - py) < threshold:
                intersection[1] = y
                intersected = True
        return intersection, intersected

    def interpolateBeamPositions(self):
        # Todo: interpolate beam positions based off of current beam positions and wall bounds
        pass

    def unsplitRacks(self):
        racks = []
        for item in self.labelList.selectedItems():
            shape = self.labelList.get_shape_from_item(item)
            if 'rack' in shape.label:
                racks.append(shape)
        # Todo: figure out how to delete shapes or annotations from the list
        for rack in racks[1:]:
            self.labelList.removeItemWidget(self.labelList.get_item_from_shape(rack))
            del rack
        points = []
        for rack in racks:
            points.append(self.qpointToPointcloud(rack.points[0]))
            points.append(self.qpointToPointcloud(rack.points[1]))
        racks[0].points[0] = self.pointcloudToQpoint(np.min(points, axis=0))
        racks[0].points[1] = self.pointcloudToQpoint(np.max(points, axis=0))
        self.updatePixmap()

    def splitRack(self, pos=None, rack=None):
        if pos is None:
            pos = self.canvas.prevPoint
        if rack is None:
            for item, shape in self.labelList.itemsToShapes:
                if 'rack' in shape.label and shape.containsPoint(pos):
                    rack = shape
                    break
        if rack is None:
            return
        newRack = rack.copy()
        dims = np.array(self.qpointToPointcloud(rack.points[0])) - np.array(self.qpointToPointcloud(rack.points[1]))
        # Todo: make this check for orientation better than checking for longest dimension
        if np.abs(dims[0]) > np.abs(dims[1]):
            rack.points[1].setX(pos.x() - 0.2)
            newRack.points[0].setX(pos.x() + 0.2)
        else:
            rack.points[1].setY(pos.y() - 0.2)
            newRack.points[0].setY(pos.y() + 0.2)
        self.addLabel(newRack)
        self.updatePixmap()
        self.setDirty()

    def splitRacks(self, selected=False):
        racks = []
        if selected:
            for item in self.labelList.selectedItems():
                shape = self.labelList.get_shape_from_item(item)
                if 'rack' in shape.label:
                    racks.append(shape)
        else:
            for _, shape in self.labelList.itemsToShapes:
                if 'rack' in shape.label:
                    racks.append(shape)
        for rack in racks:
            x_dim, y_dim = abs(rack.points[0].x() - rack.points[1].x()), abs(rack.points[0].y() - rack.points[1].y())
            x_c, y_c = (rack.points[0].x() + rack.points[1].x()) / 2.0, (rack.points[0].y() + rack.points[1].y()) / 2.0
            for _, shape in self.labelList.itemsToShapes:
                if shape.label == 'beam':
                    if rack.points[0].x() < shape.points[0].x() < rack.points[1].x():
                        if abs(y_c - shape.points[0].y()) < y_dim / 2.0 + 2.0:
                            pos = shape.points[0]
                            pos.setY(y_c)
                            self.splitRack(pos, rack)
                    elif rack.points[0].y() < shape.points[0].y() < rack.points[1].y():
                        if abs(x_c - shape.points[0].x()) < x_dim / 2.0 + 2.0:
                            pos = shape.points[0]
                            pos.setX(x_c)
                            self.splitRack(pos, rack)

    def isTwoRacks(self, type, bounds):
        dims = np.abs(bounds[1] - bounds[0])
        return (dims / 1.9 > self._config[type]).all()

    def splitTwoRacks(self, type, bounds):
        dims = np.abs(bounds[1] - bounds[0])
        bounds2 = bounds.copy()
        if abs(dims[0] - self._config[type] * 2.0) < abs(dims[1] - self._config[type] * 2.0):
            bounds[1][0] = (bounds[0][0] + bounds[1][0]) / 2.0 - 0.05
            bounds2[0][0] = (bounds2[0][0] + bounds2[1][0]) / 2.0 + 0.05
        else:
            bounds[1][1] = (bounds[0][1] + bounds[1][1]) / 2.0 - 0.05
            bounds2[0][1] = (bounds2[0][1] + bounds2[1][1]) / 2.0 + 0.05
        return bounds, bounds2

    def alignRoom(self):
        if not self.dirty:
            return True
        mb = QtWidgets.QMessageBox
        msg = self.tr('This action requires that the annotations be saved and the point cloud reloaded.'
                      'Are you sure you want to apply the changes?')
        answer = mb.question(self,
                             self.tr('Continue?'),
                             msg,
                             mb.Apply | mb.Cancel,
                             mb.Apply)
        if answer != mb.Apply:
            return

        angle = None
        from labelpc.pointcloud.Proprietary import align_room_by_walls_polygon
        for s in self.labelList.shapes:
            if s.label[:4] == 'walls' and s.shape_type == 'polygon':
                points = [(p.x(), p.y()) for p in s.points]
                angle = -align_room_by_walls_polygon(points)
                break
        if angle is None:
            return

        self.pointcloud.points[['x', 'y', 'z']] = self.pointcloud.rotate(degrees=angle)
        self.pointcloud.write(self.pointcloud.filename, overwrite=True)
        self.rotateShapes(angle)
        self.saveFile()
        self.loadFile(self.filename)

    def rotateShapes(self, angle):
        theta = np.radians(angle)
        c, s = np.cos(theta), np.sin(theta)
        rot = np.array(((c, -s), (s, c)))
        for s, shape in enumerate(self.canvas.shapes):
            for p, point in enumerate(shape.points):
                trans = self.qpointToPointcloud(point)
                trans = np.dot(trans, rot)
                self.canvas.shapes[s].points[p] = self.pointcloudToQpoint(trans)

    def render3d(self):
        if not self.pointcloud.viewer_is_ready():
            self.pointcloud.render_flag = True
            self.pointcloud.viewer = None
            self.pointcloud.render()

    def qpointToPointcloud(self, p):
        return (p.x() * self.scale + self.offset.x(),
                (self.canvas.pixmap.height() - p.y()) * self.scale + self.offset.y())

    def pointcloudToQpoint(self, p):
        x = (p[0] - self.offset.x()) / self.scale
        y = self.canvas.pixmap.height() - ((p[1] - self.offset.y()) / self.scale)
        return QtCore.QPointF(x, y)

    def highlightWalls(self):
        walls = []
        for s in self.labelList.shapes:
            if s.label[:4] == 'wall':
                walls.append([(s.points[0].x(), s.points[0].y()), (s.points[1].x(), s.points[1].y())])

    # User Dialogs #

    def loadRecent(self, filename):
        if self.mayContinue():
            self.loadFile(filename)

    def showNextSlice(self, _value=False):
        self.sliceIdx += 1
        self.updatePixmap()

    def showLastSlice(self, _value=False):
        self.sliceIdx -= 1
        self.updatePixmap()

    def openFile(self, _value=False):
        if not self.mayContinue():
            return
        path = osp.dirname(str(self.filename)) if self.filename else '.'
        formats = ['*.{}'.format(fmt.data().decode())
                   for fmt in QtGui.QImageReader.supportedImageFormats()]
        filters = self.tr("Image & Label files (%s)") % ' '.join(
            formats + ['*%s' % LabelFile.suffix])
        filename = QtWidgets.QFileDialog.getOpenFileName(
            self, self.tr('%s - Choose Image or Label file') % __appname__,
            path, filters)
        if QT5:
            filename, _ = filename
        filename = str(filename)
        if filename:
            self.loadFile(filename)

    def openPointCloud(self, _value=False):
        if not self.mayContinue():
            return
        path = osp.dirname(str(self.filename)) if self.filename else '.'
        formats = ['*.las']
        filters = self.tr("Point Cloud files (%s)") % ' '.join(
            formats + ['*%s' % LabelFile.suffix])
        filename = QtWidgets.QFileDialog.getOpenFileName(
            self, self.tr('%s - Choose Point Cloud file') % __appname__, path, filters)
        if QT5:
            filename, _ = filename
        filename = str(filename)
        if filename:
            self.loadFile(filename)

    def changeOutputDirDialog(self, _value=False):
        default_output_dir = self.output_dir
        if default_output_dir is None and self.filename:
            default_output_dir = osp.dirname(self.filename)
        if default_output_dir is None:
            default_output_dir = self.currentPath()

        output_dir = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            self.tr('%s - Save/Load Annotations in Directory') % __appname__,
            default_output_dir,
            QtWidgets.QFileDialog.ShowDirsOnly |
            QtWidgets.QFileDialog.DontResolveSymlinks,
        )
        output_dir = str(output_dir)

        if not output_dir:
            return

        self.output_dir = output_dir

        self.statusBar().showMessage(
            self.tr('%s . Annotations will be saved/loaded in %s') %
            ('Change Annotations Dir', self.output_dir))
        self.statusBar().show()

        current_filename = self.filename
        self.importDirImages(self.lastOpenDir, load=False)

        if current_filename in self.imageList:
            # retain currently selected file
            self.fileListWidget.setCurrentRow(
                self.imageList.index(current_filename))
            self.fileListWidget.repaint()

    def saveFile(self, _value=False):
        assert not self.image.isNull(), "cannot save empty image"
        if self._config['flags'] or self.hasLabels():
            if self.labelFile:
                # DL20180323 - overwrite when in directory
                self._saveFile(self.labelFile.filename)
            elif self.output_file:
                self._saveFile(self.output_file)
                self.close()
            else:
                self._saveFile(self.saveFileDialog())

    def saveFileAs(self, _value=False):
        assert not self.image.isNull(), "cannot save empty image"
        if self.hasLabels():
            self._saveFile(self.saveFileDialog())

    def saveFileDialog(self):
        caption = self.tr('%s - Choose File') % __appname__
        filters = self.tr('Label files (*%s)') % LabelFile.suffix
        if self.output_dir:
            dlg = QtWidgets.QFileDialog(
                self, caption, self.output_dir, filters
            )
        else:
            dlg = QtWidgets.QFileDialog(
                self, caption, self.currentPath(), filters
            )
        dlg.setDefaultSuffix(LabelFile.suffix[1:])
        dlg.setAcceptMode(QtWidgets.QFileDialog.AcceptSave)
        dlg.setOption(QtWidgets.QFileDialog.DontConfirmOverwrite, False)
        dlg.setOption(QtWidgets.QFileDialog.DontUseNativeDialog, False)
        basename = osp.basename(osp.splitext(self.filename)[0])
        if self.output_dir:
            default_labelfile_name = osp.join(
                self.output_dir, basename + LabelFile.suffix
            )
        else:
            default_labelfile_name = osp.join(
                self.currentPath(), basename + LabelFile.suffix
            )
        filename = dlg.getSaveFileName(
            self, self.tr('Choose File'), default_labelfile_name,
            self.tr('Label files (*%s)') % LabelFile.suffix)
        if QT5:
            filename, _ = filename
        filename = str(filename)
        return filename

    def _saveFile(self, filename):
        if filename and self.saveLabels(filename):
            self.addRecentFile(filename)
            self.setClean()

    def closeFile(self, _value=False):
        if not self.mayContinue():
            return
        self.resetState()
        self.setClean()
        self.toggleActions(False)
        self.canvas.setEnabled(False)
        self.actions.saveAs.setEnabled(False)

    def getLabelFile(self):
        if self.filename.lower().endswith('.json'):
            label_file = self.filename
        else:
            label_file = osp.splitext(self.filename)[0] + '.json'

        return label_file

    def deleteFile(self):
        mb = QtWidgets.QMessageBox
        msg = self.tr('You are about to permanently delete this label file, '
                      'proceed anyway?')
        answer = mb.warning(self, self.tr('Attention'), msg, mb.Yes | mb.No)
        if answer != mb.Yes:
            return

        label_file = self.getLabelFile()
        if osp.exists(label_file):
            os.remove(label_file)
            logger.info('Label file is removed: {}'.format(label_file))

            item = self.fileListWidget.currentItem()
            item.setCheckState(Qt.Unchecked)

            self.resetState()

    # Message Dialogs. #
    def hasLabels(self):
        if not self.labelList.itemsToShapes:
            self.errorMessage(
                'No objects labeled',
                'You must label at least one object to save the file.')
            return False
        return True

    def hasLabelFile(self):
        if self.filename is None:
            return False

        label_file = self.getLabelFile()
        return osp.exists(label_file)

    def mayContinue(self):
        if not self.dirty:
            return True
        mb = QtWidgets.QMessageBox
        msg = self.tr('Save annotations to "{}" before closing?').format(
            self.filename)
        answer = mb.question(self,
                             self.tr('Save annotations?'),
                             msg,
                             mb.Save | mb.Discard | mb.Cancel,
                             mb.Save)
        if answer == mb.Discard:
            return True
        elif answer == mb.Save:
            self.saveFile()
            return True
        else:  # answer == mb.Cancel
            return False

    def errorMessage(self, title, message):
        return QtWidgets.QMessageBox.critical(
            self, title, '<p><b>%s</b></p>%s' % (title, message))

    def currentPath(self):
        return osp.dirname(str(self.filename)) if self.filename else '.'

    def toggleKeepPrevMode(self):
        self._config['keep_prev'] = not self._config['keep_prev']

    def deleteSelectedShape(self):
        yes, no = QtWidgets.QMessageBox.Yes, QtWidgets.QMessageBox.No
        msg = self.tr(
            'You are about to permanently delete {} polygons, '
            'proceed anyway?'
        ).format(len(self.canvas.selectedShapes))
        if yes == QtWidgets.QMessageBox.warning(
                self, self.tr('Attention'), msg,
                yes | no):
            self.remLabels(self.canvas.deleteSelected())
            self.setDirty()
            if self.noShapes():
                for action in self.actions.onShapesPresent:
                    action.setEnabled(False)

    def copyShape(self):
        self.canvas.endMove(copy=True)
        self.labelList.clearSelection()
        for shape in self.canvas.selectedShapes:
            self.addLabel(shape)
        self.setDirty()

    def moveShape(self):
        self.canvas.endMove(copy=False)
        self.setDirty()

    def highlightPointsInLabel(self, shape):
        if shape.label == 'beam':
            point = np.array(self.qpointToPointcloud(shape.points[0]))
            box = [point - 0.1, point + 0.1]
        elif shape.label == 'pole':
            point = np.array(self.qpointToPointcloud(shape.points[0]))
            box = [point - 0.05, point + 0.05]
        elif 'rack' in shape.label:
            box = [self.qpointToPointcloud(shape.points[0]), self.qpointToPointcloud(shape.points[1])]
        else:
            return
        inbox = self.pointcloud.in_box_2d(box)
        if not np.sum(inbox):
            return
        self.pointcloud.highlight(self.pointcloud.select(inbox, highlighted=False))

    def updateSelectedLabelWithHighlightedPoints(self):
        items = self.labelList.selectedItems()
        if len(items) != 1 or not self.pointcloud.viewer_is_ready():
            return
        shape = self.labelList.get_shape_from_item(items[0])
        points = self.pointcloud.points.loc[self.pointcloud.viewer.get('selected')][['x', 'y']].values
        if 'rack' in shape.label:
            shape.points[0] = self.pointcloudToQpoint(points.min(axis=0))
            shape.points[1] = self.pointcloudToQpoint(points.max(axis=0))
        elif shape.label == 'beam' or shape.label == 'pole':
            shape.points[0] = self.pointcloudToQpoint((points.min(axis=0) + points.max(axis=0)) / 2.0)

    def openDirDialog(self, _value=False, dirpath=None):
        if not self.mayContinue():
            return

        defaultOpenDirPath = dirpath if dirpath else '.'
        if self.lastOpenDir and osp.exists(self.lastOpenDir):
            defaultOpenDirPath = self.lastOpenDir
        else:
            defaultOpenDirPath = osp.dirname(self.filename) \
                if self.filename else '.'

        targetDirPath = str(QtWidgets.QFileDialog.getExistingDirectory(
            self,
            self.tr('%s - Open Directory') % __appname__,
            defaultOpenDirPath,
            QtWidgets.QFileDialog.ShowDirsOnly |
            QtWidgets.QFileDialog.DontResolveSymlinks))
        self.importDirImages(targetDirPath)

    @property
    def imageList(self):
        lst = []
        for i in range(self.fileListWidget.count()):
            item = self.fileListWidget.item(i)
            lst.append(item.text())
        return lst

    def importDirImages(self, dirpath, pattern=None, load=True):
        self.actions.openNextImg.setEnabled(True)
        self.actions.openPrevImg.setEnabled(True)

        if not self.mayContinue() or not dirpath:
            return

        self.lastOpenDir = dirpath
        self.filename = None
        self.fileListWidget.clear()
        for filename in self.scanAllImages(dirpath):
            if pattern and pattern not in filename:
                continue
            label_file = osp.splitext(filename)[0] + '.json'
            if self.output_dir:
                label_file_without_path = osp.basename(label_file)
                label_file = osp.join(self.output_dir, label_file_without_path)
            item = QtWidgets.QListWidgetItem(filename)
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            if QtCore.QFile.exists(label_file) and \
                    LabelFile.is_label_file(label_file):
                item.setCheckState(Qt.Checked)
            else:
                item.setCheckState(Qt.Unchecked)
            self.fileListWidget.addItem(item)
        self.openNextImg(load=load)

    def scanAllImages(self, folderPath):
        extensions = ['.%s' % fmt.data().decode("ascii").lower()
                      for fmt in QtGui.QImageReader.supportedImageFormats()]
        images = []

        for root, dirs, files in os.walk(folderPath):
            for file in files:
                if file.lower().endswith(tuple(extensions)):
                    relativePath = osp.join(root, file)
                    images.append(relativePath)
        images.sort(key=lambda x: x.lower())
        return images