import TImageRead
import numpy as np
import os
import TPSParabola
from math import gcd


def _spectrum_particle_label(app):
    try:
        if int(getattr(app, "A", 0)) == 1 and int(getattr(app, "Z", 0)) == 1:
            return "protons"
    except (TypeError, ValueError):
        pass
    return "ions"


def _set_spectrum_axes(app):
    particle = _spectrum_particle_label(app)
    app.ax_spec.set_xlabel("Energy (MeV)", fontsize=14, labelpad=6)
    app.ax_spec.set_ylabel(
        rf"$d^2N/(dE\,d\Omega)$ ({particle} MeV$^{{-1}}$ sr$^{{-1}}$)",
        fontsize=14,
        labelpad=8
    )
    app.ax_spec.minorticks_on()
    app.ax_spec.tick_params(direction="in", which="both", top=True, right=True, labelsize=12)


def _plot_spectrum_line(app, x, y, **kwargs):
    if getattr(app, "spectrumScale", "log") == "linear":
        app.ax_spec.plot(x, y, **kwargs)
    else:
        app.ax_spec.semilogy(x, y, **kwargs)


def _finish_spectrum_layout(app):
    app.ax_spec.legend(frameon=False, fontsize=10)
    app.specFig.subplots_adjust(left=0.20, bottom=0.18, right=0.97, top=0.96)


def DrawImage(app):
    if os.path.isfile(app.imageFileName):
        app.imageFig.clf()
        pic = getattr(app, "displayImage", app.imageData)

        app.ax_image = app.imageFig.subplots()
        if app.zoomed == False:
            img = app.ax_image.imshow(pic, cmap=app.cmap, vmin=app.colormin, vmax=app.colormax)
        else:
            img = app.ax_image.imshow(app.zoomPic, cmap=app.cmap, vmin=app.colormin, vmax=app.colormax)
        app.ax_image.invert_yaxis()
        app.imageFig.colorbar(img, ax=app.ax_image)
        if app.imageCursor == None:
            pass
        else:
            if app.zoomed == False:
                app.ax_image.axvline(app.imageCursor, linewidth=1, color='black')
            else:
                app.ax_image.axvline(app.zoomImageCursor, linewidth=1, color='black')

        if hasattr(app, "drawManualMaskOverlay"):
            app.drawManualMaskOverlay()

        view = app.zoomPic if app.zoomed else pic
        app.ax_image.set_xlim(-0.5, view.shape[1] - 0.5)
        app.ax_image.set_ylim(view.shape[0] - 0.5, -0.5)

        app.imageCanvas.draw()
        print(f"[TDraw] 绘制图像完成，使用displayImage，形状: {pic.shape}")


def DrawSpec(app):
    if os.path.isfile(app.imageFileName):
        app.specFig.clf()
        app.ax_spec = app.specFig.subplots()
        app.ax_spec.set_xlim(app.specEmin, app.specEmax)
        app.ax_spec.set_ylim(app.specdNdEmin, app.specdNdEmax)
        _set_spectrum_axes(app)
        if hasattr(app, "specDataRaw"):
            _plot_spectrum_line(app, app.specDataRaw[0], app.specDataRaw[1], alpha=0.45, linewidth=1.0,
                                label="Raw center-band")
        if hasattr(app, "noiseSpec"):
            _plot_spectrum_line(app, app.noiseSpec[0], app.noiseSpec[1], alpha=0.55, linewidth=1.0,
                                linestyle='--', label="Raw background")
        if hasattr(app, "specDataNet"):
            _plot_spectrum_line(app, app.specDataNet[0], app.specDataNet[1], alpha=0.65, linewidth=1.1,
                                label="Background-subtracted")
        _plot_spectrum_line(app, app.specData[0], app.specData[1], linewidth=2.0,
                            label="Smoothed background-subtracted")
        _finish_spectrum_layout(app)
        # app.ax_spec.plot(app.specData[0], app.specData[1])
        # app.ax_spec.plot(app.noiseSpec[0], app.noiseSpec[1])

        try:
            show_cursor = float(app.specCursor) > 0   # 仅画有效的手动光标（>0）
        except (TypeError, ValueError):
            show_cursor = False
        if show_cursor:
            app.ax_spec.axvline(app.specCursor, linewidth=1.2, color='black')
            app.ui.lineEdit_cursor.setText(str(np.round(app.specCursor, 2)))
            # app.pv.put(np.round(app.specCursor,2))
        app.specCanvas.draw()


def DrawParabola(app):
    if app.zoomed == False:
        app.ax_image.plot(app.TPSParabola[0], app.TPSParabola[1], alpha=0.5, linewidth=2, color='red')
        if getattr(app, 'backgroundMode', None) == "offset" and hasattr(app, 'backgroundParabola'):
            app.ax_image.plot(app.backgroundParabola[0], app.backgroundParabola[1], alpha=0.7,
                              linewidth=1.5, color='lime', linestyle='--')
    else:
        app.ax_image.plot(app.zoomPara[0], app.zoomPara[1], alpha=0.5, linewidth=2, color='red')
        if getattr(app, 'backgroundMode', None) == "offset" and hasattr(app, 'zoomBackgroundPara'):
            app.ax_image.plot(app.zoomBackgroundPara[0], app.zoomBackgroundPara[1], alpha=0.7,
                              linewidth=1.5, color='lime', linestyle='--')
    DrawParticleLabels(app)
    app.imageCanvas.draw()


def DrawSelectedParticleLabels(app):
    DrawParticleLabels(app)
    app.imageCanvas.draw()


def ParticleLabelList():
    return [
        (1, 1, "H-1"),
        (12, 6, "C-6"),
        (16, 8, "O-8"),
        (16, 7, "O-7"),
        (12, 5, "C-5"),
        (16, 6, "O-6"),
        (12, 4, "C-4"),
        (16, 5, "O-5"),
        (12, 3, "C-3"),
        (16, 4, "O-4"),
        (12, 2, "C-2"),
        (12, 1, "C-1"),
    ]


def DrawParticleLabels(app):
    if not getattr(app, "isShowParticleLabel", False):
        return
    if not hasattr(app, "specData") or len(app.specData) == 0:
        return

    colors = [
        "#ff3b30", "#007aff", "#34c759", "#ff9500",
        "#af52de", "#00c7be", "#ff2d55", "#5856d6",
        "#8e8e93", "#64d2ff", "#bf5af2", "#ffd60a",
    ]
    boundary = app.imageData.shape[1]
    energy = np.asarray(app.specData[0], dtype=float)
    energy = energy[np.isfinite(energy) & (energy > 0)]
    if energy.size == 0:
        return
    for i, (A, Z, labels) in enumerate(SelectedParticleGroups(app)):
        label = " / ".join(labels)
        line = TPSParabola.Parabola(app.TPS, energy, app.X0, app.Y0, A, Z, boundary)
        if app.zoomed:
            line = TPSParabola.ZoomPara(line, app.zoomToOrigin, app.zoomPic.shape[1])
        DrawParticleLabelOnly(app, line, label, colors[i % len(colors)])


def SelectedParticleGroups(app):
    enabled = getattr(app, "enabledParticleLabels", None)
    groups = {}
    for A, Z, label in ParticleLabelList():
        if enabled is not None and label not in enabled:
            continue
        divisor = gcd(int(A), int(Z))
        key = (int(A) // divisor, int(Z) // divisor)
        if key not in groups:
            groups[key] = {"A": A, "Z": Z, "labels": []}
        groups[key]["labels"].append(label)
    return [(item["A"], item["Z"], item["labels"]) for item in groups.values()]


def DrawParticleLabelOnly(app, line, label, color):
    if line is None or len(line) < 2 or line[0].size == 0:
        return

    x = np.asarray(line[0], dtype=float)
    y = np.asarray(line[1], dtype=float)
    xlim = app.ax_image.get_xlim()
    ylim = app.ax_image.get_ylim()
    xmin, xmax = min(xlim), max(xlim)
    ymin, ymax = min(ylim), max(ylim)

    margin_x = max(8, 0.02 * (xmax - xmin))
    margin_y = max(8, 0.02 * (ymax - ymin))
    visible = (
        np.isfinite(x) & np.isfinite(y) &
        (x >= xmin + margin_x) & (x <= xmax - margin_x) &
        (y >= ymin + margin_y) & (y <= ymax - margin_y)
    )
    idxs = np.flatnonzero(visible)
    if idxs.size == 0:
        return

    # The trajectory array runs from higher energy to lower energy, so choose
    # a visible point near the low-energy end while keeping away from the edge.
    label_idx = idxs[min(idxs.size - 1, max(0, int(idxs.size * 0.85)))]
    app.ax_image.text(
        x[label_idx], y[label_idx], label,
        color='white', fontsize=11, fontweight='bold',
        ha='left', va='center',
        bbox=dict(facecolor=color, alpha=0.78, edgecolor='none', boxstyle='round,pad=0.2')
    )


def DrawOrigin(app):
    if app.zoomed == False:
        hLine = np.array([np.arange(app.X0 - 50, app.X0 + 51), np.linspace(app.Y0, app.Y0, 101, dtype=int)])
        vLine = np.array([np.linspace(app.X0, app.X0, 101, dtype=int), np.arange(app.Y0 - 50, app.Y0 + 51)])
        app.ax_image.plot(hLine[0], hLine[1], linewidth=1, color='black')
        app.ax_image.plot(vLine[0], vLine[1], linewidth=1, color='black')
        app.imageCanvas.draw()
    else:
        pass


def DrawAnalysingSpec(app):
    app.specFig.clf()
    app.ax_spec = app.specFig.subplots()
    app.ax_spec.set_xlim(app.specEmin, app.specEmax)
    app.ax_spec.set_ylim(app.specdNdEmin, app.specdNdEmax)
    _set_spectrum_axes(app)
    if app.analysingDataList == []:
        pass
    else:
        for i in range(len(app.analysingDataList)):
            if isinstance(app.cutoffEnergyList[i], str):
                _plot_spectrum_line(app, app.analysingDataList[i][0], app.analysingDataList[i][1], label=str(
                    os.path.splitext(os.path.basename(app.analysingFileList[i]))[0]) + '--NaN')
            else:
                _plot_spectrum_line(app, app.analysingDataList[i][0], app.analysingDataList[i][1], label=str(
                    os.path.splitext(os.path.basename(app.analysingFileList[i]))[0]) + '--' + str(
                    app.cutoffEnergyList[i]) + 'MeV')
        _finish_spectrum_layout(app)
        app.ax_spec.axvline(app.specCursor, linewidth=1.2, color='black')
        app.ui.lineEdit_cursor.setText(str(np.round(app.specCursor, 2)))
    app.specCanvas.draw()
