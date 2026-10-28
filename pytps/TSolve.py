import TDraw, SolveSpectrum, AutoCutoffEnergy, TPSParabola, TImageRead, TFilter
import numpy as np
import os


def Solve(app):
    if os.path.isfile(app.imageFileName) and app.isAnalysing == False:
        app.zoomed = False
        raw_img, app.is8bitImage = TImageRead.readImage(app.imageFileName)

        filterParams = {
            'medianSize': app.medianSize,
            'medianIterations': app.medianIterations,
            'morphologicalSize': app.morphologicalSize,
            'rollingBallRadius': app.rollingBallRadius,  # 恢复滚球法参数
            'fadeRadius': app.fadeRadius if hasattr(app, 'fadeRadius') else 20,  # 软掩膜羽化半径
            # 形态学分割参数（简化版）
            'thresholdValue': getattr(app, 'thresholdValue', 500),
            'morphKernelSize': getattr(app, 'morphKernelSize', 3),
            'showSegmentationMask': getattr(app, 'showSegmentationMask', False),
            # 轨迹保护滤波需要的TPS参数（保留向后兼容）
            'X0': app.X0,
            'Y0': app.Y0,
            'A': app.A,
            'Z': app.Z,
            'Emin': app.Emin,
            'Emax': app.Emax,
            'dY': app.dY,
            'TPS': app.TPS,
            'protectionWidth': app.protectionWidth if hasattr(app, 'protectionWidth') else int(app.dY * 3),
            'aggressiveSize': app.aggressiveSize if hasattr(app, 'aggressiveSize') else 15,
            'gentleSize': app.gentleSize if hasattr(app, 'gentleSize') else 5,
            'analysisPreprocess': getattr(app, 'analysisPreprocess', 'hot_pixel'),
            'hotPixelMedianSize': getattr(app, 'analysisMedianSize', 3),
        }

        app.rawImage = np.flipud(raw_img)
        analysis_img = TFilter.makeAnalysisImage(raw_img, filterParams)
        display_img = TFilter.applyFilter(raw_img, app.filterMode, filterParams)
        app.analysisImage = np.flipud(analysis_img)
        app.displayImage = np.flipud(display_img)
        app.baseDisplayImage = app.displayImage.copy()

        # Backward-compatible display image. Quantitative extraction uses analysisImage.
        app.imageData = app.displayImage
        app.zoomPic = app.displayImage

        print(f"[TSolve] 图像分流完成: raw={raw_img.shape}, analysis={analysis_img.shape}, display={display_img.shape}")
        app.specDataRaw, app.noiseSpec, app.spectrumStats = SolveSpectrum.SolveSpectrumSideband(
            app.analysisImage, app.X0, app.Y0, app.A, app.Z, app.Emin,
            app.Emax, app.dY, app.MCPPath, app.TPS, app.is8bitImage,
            background_mode=getattr(app, 'backgroundMode', 'sideband'),
            background_offset=getattr(app, 'backgroundOffset', None),
            background_width=getattr(app, 'backgroundWidth', None),
            return_stats=True
        )
        app.specDataNet = app.spectrumStats["net_spectrum"]
        app.specData = SolveSpectrum.SmoothSpectrumDisplayRaw(
            app.specDataNet,
            smooth_window=getattr(app, 'spectrumSmoothWindow', 9)
        )
        app.TPSParabola = TPSParabola.Parabola(app.TPS, app.specData[0], app.X0, app.Y0, app.A, app.Z,
                                               app.imageData.shape[1])
        app.backgroundParabola = TPSParabola.OffsetParabola(
            app.TPS, app.specData[0], app.X0, app.Y0, app.A, app.Z,
            app.imageData.shape[1], getattr(app, 'backgroundOffset', -18)
        )
        # 截止能量只认手动光标（用户要求：不自动寻找）。
        # 批量分析模式仍用 CutoffEnergySNR 自动值（见下方 isAnalysing 分支）。
        if getattr(app, 'isManualCursor', False) and \
                isinstance(app.specCursor, (int, float)) and app.specCursor > 0:
            app.imageCursor = TPSParabola.CutoffLine(app.TPS, app.specCursor, app.X0, app.A, app.Z)
        TDraw.DrawImage(app)
        TDraw.DrawSpec(app)

        if app.isShowParabola:
            TDraw.DrawParabola(app)

        TDraw.DrawOrigin(app)
        if getattr(app, 'isManualCursor', False) and \
                isinstance(app.specCursor, (int, float)) and app.specCursor > 0:
            app.ui.lineEdit_cutoff.setText(str(np.round(app.specCursor, 2)))
        else:
            app.ui.lineEdit_cutoff.setText("")   # 未点击光标前留空，不显示自动值
        # app.pv.put(app.cutoffEnergy)

        if app.isAutoSave == True:
            app.saveImage()
            app.saveSpec()

    if app.isAnalysing == True:
        app.analysingDataList = []
        app.cutoffEnergyList = []
        if app.analysingFileList == []:
            app.specCursor = 0.0
            TDraw.DrawAnalysingSpec(app)
        else:
            for image in app.analysingFileList:
                raw_img, app.is8bitImage = TImageRead.readImage(image)

                filterParams = {
                    'medianSize': app.medianSize,
                    'medianIterations': app.medianIterations,
                    'morphologicalSize': app.morphologicalSize,
                    'rollingBallRadius': app.rollingBallRadius,  # 恢复滚球法参数
                    'fadeRadius': app.fadeRadius if hasattr(app, 'fadeRadius') else 20,  # 软掩膜羽化半径
                    # 形态学分割参数（简化版）
                    'thresholdValue': getattr(app, 'thresholdValue', 500),
                    'morphKernelSize': getattr(app, 'morphKernelSize', 3),
                    'showSegmentationMask': getattr(app, 'showSegmentationMask', False),
                    # 轨迹保护滤波需要的TPS参数（保留向后兼容）
                    'X0': app.X0,
                    'Y0': app.Y0,
                    'A': app.A,
                    'Z': app.Z,
                    'Emin': app.Emin,
                    'Emax': app.Emax,
                    'dY': app.dY,
                    'TPS': app.TPS,
                    'protectionWidth': app.protectionWidth if hasattr(app, 'protectionWidth') else int(app.dY * 3),
                    'aggressiveSize': app.aggressiveSize if hasattr(app, 'aggressiveSize') else 15,
                    'gentleSize': app.gentleSize if hasattr(app, 'gentleSize') else 5,
                    'analysisPreprocess': getattr(app, 'analysisPreprocess', 'hot_pixel'),
                    'hotPixelMedianSize': getattr(app, 'analysisMedianSize', 3),
                }
                analysis_img = TFilter.makeAnalysisImage(raw_img, filterParams)
                data = np.flipud(analysis_img)
                spec_raw, noise, stats = SolveSpectrum.SolveSpectrumSideband(
                    data, app.X0, app.Y0, app.A, app.Z, app.Emin, app.Emax,
                    app.dY, app.MCPPath, app.TPS, app.is8bitImage,
                    background_mode=getattr(app, 'backgroundMode', 'sideband'),
                    background_offset=getattr(app, 'backgroundOffset', None),
                    background_width=getattr(app, 'backgroundWidth', None),
                    return_stats=True
                )
                spec = SolveSpectrum.SmoothSpectrumDisplayRaw(
                    stats["net_spectrum"],
                    smooth_window=getattr(app, 'spectrumSmoothWindow', 9)
                )
                app.analysingDataList.append(spec)
                app.cutoffEnergyList.append(
                    SolveSpectrum.CutoffEnergySNR(
                        spec, noise, app.Emin, app.Emax,
                        snr_threshold=getattr(app, 'cutoffSnrThreshold', 3.0),
                        consecutive=getattr(app, 'cutoffConsecutiveBins', 3),
                        smooth_window=getattr(app, 'spectrumSmoothWindow', 9)
                    ))
            app.specCursor = 0.0
            TDraw.DrawAnalysingSpec(app)
