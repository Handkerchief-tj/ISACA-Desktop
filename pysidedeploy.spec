[app]

title = ISACA Desktop
project_dir = .
input_file = release_entry.py
exec_directory = build\standalone
project_file =
icon =

[python]

python_path = .venv-build\Scripts\python.exe
packages = Nuitka==4.1.1,ordered-set==4.1.0,zstandard==0.25.0
android_packages =

[qt]

qml_files =
excluded_qml_plugins = QtQuick3D,QtCharts,QtTest,QtSensors
modules = Core,Gui,Network,OpenGL,Positioning,PrintSupport,Qml,QmlMeta,QmlModels,QmlWorkerScript,Quick,QuickWidgets,Svg,SvgWidgets,WebChannel,WebEngineCore,WebEngineWidgets,Widgets
plugins = accessiblebridge,generic,iconengines,imageformats,networkaccess,networkinformation,platforminputcontexts,platforms,platformthemes,position,printsupport,scenegraph,styles,tls,vectorimageformats

[android]

wheel_pyside =
wheel_shiboken =
plugins =

[nuitka]

macos.permissions =
mode = standalone
extra_args = --assume-yes-for-downloads --msvc=latest --windows-console-mode=attach --include-package=SLiCAP --include-package=sfg_prototype --include-package=scipy._external.array_api_compat --include-module=matplotlib.backends.backend_svg --include-package-data=SLiCAP --include-package-data=isaca_desktop --output-filename=ISACA.exe --noinclude-qt-translations

[buildozer]

mode = debug
recipe_dir =
jars_dir =
ndk_path =
sdk_path =
local_libs =
arch =
