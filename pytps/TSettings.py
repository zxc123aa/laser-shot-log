import numpy as np
import os
import CustomColormaps


class TPS(object):
    def __init__(self):
        data = np.genfromtxt("./TPS_Settings.ini", delimiter='\t', encoding='utf-8')
        data = data[..., 1]
        self.B = data[0]
        self.U = data[1]
        self.EMGain = data[2]
        self.S1 = data[3]
        self.Res = data[4]
        self.L = data[5]
        self.D = data[6]
        self.L1 = data[7]
        self.L2 = data[8]
        self.L3 = data[9]
        self.theta = data[10]
        self.d = data[11]
        self.D1 = data[12]
        self.L0 = data[13]
        self.QE = data[14]


def LoadMCPData(path):
    MCPdata = np.genfromtxt(path, delimiter='\t', encoding='utf-8')
    return MCPdata


def TPSinit(app):
    path_data = np.genfromtxt("./PyTPS_init.ini", delimiter='\t', dtype=str, encoding='utf-8')
    float_data = np.genfromtxt("./PyTPS_init.ini", delimiter='\t', encoding='utf-8')
    int_data = np.genfromtxt("./PyTPS_init.ini", delimiter='\t', dtype=np.int64, encoding='utf-8')
    app.imagePath, app.savePath, app.MCPPath = path_data[0:3, 1]
    app.X0, app.Y0, app.dY, app.colormin, app.colormax = int_data[3:8, 1]
    app.Emin, app.Emax, app.specEmin, app.specEmax, app.specdNdEmin, app.specdNdEmax, app.specWindow = float_data[8:15, 1]

    # 加载滤波配置（新增）
    app.filterMode = path_data[15, 1]  # 显示滤波：none, median3, median5；旧protected会在UI中迁移
    app.medianSize = int_data[16, 1]   # 中值滤波窗口大小
    app.medianIterations = int_data[17, 1]  # 中值滤波迭代次数
    app.morphologicalSize = int_data[18, 1]  # 形态学结构元素大小（新增）
    app.rollingBallRadius = int_data[19, 1]  # 滚球法半径
    app.protectionWidth = int_data[20, 1]  # 轨迹保护宽度（新增）
    app.aggressiveSize = int_data[21, 1]  # 轨迹外激进滤波窗口（新增）
    app.gentleSize = int_data[22, 1]  # 轨迹内温和滤波窗口（新增）

    # 兼容旧配置文件：fadeRadius可能不存在
    try:
        app.fadeRadius = int_data[23, 1]  # 软掩膜羽化半径（新增）
    except IndexError:
        app.fadeRadius = 20  # 默认值
        print("[TSettings] 警告：配置文件缺少fadeRadius参数，使用默认值20")

    app.TPS = TPS()
    app.imageFileName = "."


def getList(app):
    configured_path = os.fspath(app.imagePath)
    fallback_path = os.path.join(".", "sample_data", "PW-TPS-stability")

    if not os.path.isdir(configured_path):
        if os.path.isdir(fallback_path):
            print(f"[TSettings] 图像路径不存在：{configured_path}，已切换到样例数据：{fallback_path}")
            app.imagePath = fallback_path
        else:
            print(f"[TSettings] 图像路径不存在：{configured_path}，样例数据也不存在，使用当前目录。")
            app.imagePath = "."

    fileList = os.listdir(app.imagePath)
    imageList = []
    for i in range(len(fileList)):
        if fileList[i].endswith(".tif") or fileList[i].endswith(".tiff") or fileList[i].endswith(".jpg") or fileList[
            i].endswith(".png") or fileList[i].endswith(".raw"):
            imageList.append(fileList[i])
        else:
            pass

    app.ui.listWidget.addItems(imageList)


def particalList(app):
    app.ui.comboBox_particals.addItems(
        ["H-1", "C-6", "O-8", "O-7", "C-5", "O-6", "C-4", "O-5", "C-3", "O-4", "C-2", "C-1", "自定义..."])
    app.A = 1
    app.Z = 1
    app.ui.lineEdit_A.setText(str(app.A))
    app.ui.lineEdit_Z.setText(str(app.Z))
    app.ui.lineEdit_A.setReadOnly(True)
    app.ui.lineEdit_Z.setReadOnly(True)


def colormapList(app):
    app.ui.comboBox_colormap.addItems(
        ["partical", "field", "jet", "hsv", "hot", "cool", "gray", "spring", "summer", "autumn", "winter", "bone",
         "pink", "copper"])
    app.cmap = CustomColormaps.particals
