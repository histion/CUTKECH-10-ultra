# 任务栏通知区功率图标 + 一行式悬浮功率面板（零依赖：只用 .NET 自带 WinForms/Drawing）。
#
#   powershell -NoProfile -STA -ExecutionPolicy Bypass -File tray.ps1 -Port <port> [-Mode total|all|panel]
#
# 每秒轮询本机 /api/state。显示模式（跟随 config.trayMode，改了立刻生效、不用重启托盘进程）：
#   total —— 托盘图标只显示总功率
#   all   —— 托盘图标轮流显示 总 → C1 → C2 → C3 → A（每秒一帧，四口各自配色）
#   panel —— 额外显示一行式悬浮面板（贴任务栏右下、置顶、可拖动），
#            一行同时显示 总 + 四口功率；托盘图标此时显示总功率
# 右键菜单（图标和面板都有）可打开窗口、切换显示模式或退出。连续多次取不到状态
# （说明服务已退出）就自动结束，不会在服务被关掉后留下孤儿图标/面板。
param(
  [int]$Port = 0,
  [int]$PollMs = 1000,
  [string]$Mode = 'total'
)
$ErrorActionPreference = 'Stop'
if ($Port -le 0) { exit 2 }
$base = "http://127.0.0.1:$Port"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class TBWin {
  [DllImport("user32.dll", CharSet=CharSet.Auto)] public static extern IntPtr FindWindow(string cls, string win);
  [DllImport("user32.dll", CharSet=CharSet.Auto)] public static extern IntPtr FindWindowEx(IntPtr parent, IntPtr after, string cls, string win);
  [DllImport("user32.dll")] public static extern IntPtr SetParent(IntPtr child, IntPtr parent);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern IntPtr GetParent(IntPtr h);
  public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
}
"@

# ---------------------------------------------------------------- 图标绘制
$bmp = New-Object System.Drawing.Bitmap 16, 16
$sf  = New-Object System.Drawing.StringFormat
$sf.Alignment = 'Center'
$sf.LineAlignment = 'Center'
$fontBig   = New-Object System.Drawing.Font('Segoe UI', 9, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
$fontMid   = New-Object System.Drawing.Font('Segoe UI', 7, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
$fontSmall = New-Object System.Drawing.Font('Segoe UI', 6, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
$fontTag   = New-Object System.Drawing.Font('Segoe UI', 5, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)

# 每个口的标识色（总功率 = 青蓝）；面板上同一套配色
$portColor = @{
  'C1' = [System.Drawing.Color]::FromArgb(255, 56, 189, 248)    # 青
  'C2' = [System.Drawing.Color]::FromArgb(255, 52, 211, 153)    # 绿
  'C3' = [System.Drawing.Color]::FromArgb(255, 251, 191, 36)    # 琥珀
  'A'  = [System.Drawing.Color]::FromArgb(255, 167, 139, 250)   # 紫
}
$colTotal  = $portColor['C1']
$colIdle   = [System.Drawing.Color]::FromArgb(255, 130, 140, 160)
$colBright = [System.Drawing.Color]::FromArgb(255, 235, 240, 248)

$script:prevHicon = [IntPtr]::Zero
$script:lastKey   = ''
$script:mode      = $(if ($Mode -in @('all','panel')) { $Mode } else { 'total' })
$script:frameSeq  = 0

function New-PowerIconFrame([string]$label, [double]$watts, [string]$state) {
  # 颜色：连接时 总=青蓝 / 各口用标识色；未连接一律灰
  if ($state -ne 'connected') {
    $col = $colIdle
  } elseif ($label -eq '') {
    $col = $colTotal
  } else {
    $col = $portColor[$label]
  }

  $text = '-'
  $font = $fontBig
  if ($watts -ge 1000) { $text = '{0:0.#}k' -f ($watts / 1000); $font = $fontSmall }
  elseif ($watts -ge 100) { $text = '{0:0}' -f $watts; $font = $fontSmall }
  elseif ($watts -gt 0) { $text = '{0:0}' -f $watts; $font = $fontMid }

  # 只在显示内容真的变了时才重画 —— 每次重画都会占一个 GDI 句柄
  $key = "$label|$text|$state"
  if ($key -eq $script:lastKey) { return }
  $script:lastKey = $key

  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.SmoothingMode = 'AntiAlias'
  $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
  $g.Clear([System.Drawing.Color]::FromArgb(255, 8, 12, 22))
  $pen = New-Object System.Drawing.Pen -ArgumentList ([System.Drawing.Color]::FromArgb(200, $col.R, $col.G, $col.B)), 1
  $g.DrawRectangle($pen, 0, 0, 15, 15)
  $brush = New-Object System.Drawing.SolidBrush $col
  if ($label -eq '') {
    # 总功率：单行大数字
    $g.DrawString($text, $font, $brush, (New-Object System.Drawing.RectangleF 0, 0, 16, 16), $sf)
  } else {
    # 单口：上排小标签、下排数值
    $g.DrawString($label, $fontTag, $brush, (New-Object System.Drawing.RectangleF 0, 0, 16, 6), $sf)
    $g.DrawString($text, $font, $brush, (New-Object System.Drawing.RectangleF 0, 5, 16, 11), $sf)
  }
  $g.Dispose()
  $pen.Dispose()
  $brush.Dispose()

  $h = $bmp.GetHicon()
  $ni.Icon = [System.Drawing.Icon]::FromHandle($h)
  # 旧图标句柄在换上新的之后就可以销毁了，避免长时间运行积累 GDI 句柄
  if ($script:prevHicon -ne [IntPtr]::Zero) {
    try { [void][System.Drawing.Icon]::DestroyIcon($script:prevHicon) } catch {}
  }
  $script:prevHicon = $h
}

# ---------------------------------------------------------------- 悬浮面板
# 一行式：  总 34.2W   C1 15.6W   C2 9.7W   C3 18.5W   A 0.0W
$script:panelFont  = New-Object System.Drawing.Font('Microsoft YaHei UI', 12, [System.Drawing.FontStyle]::Bold)
$script:panelSegs  = @()
$script:panelPlaced = $false
$script:panelDragging = $false
$script:panelDragDX = 0
$script:panelDragDY = 0
$script:panelEmbedded = $false      # 已挂进任务栏
$script:panelAnchorRight = 0        # 任务栏内右边界（屏幕坐标）
$script:panelTBTop = 0
$script:panelTBH = 0

$panel = New-Object System.Windows.Forms.Form
$panel.Text = 'CuktechPowerPanel'          # FindWindow 用这个标题定位
$panel.FormBorderStyle = 'None'
$panel.ShowInTaskbar = $false
$panel.TopMost = $true
# 背景完全透明（ TransparencyKey = BackColor ），只显示文字本身
$panel.BackColor = [System.Drawing.Color]::FromArgb(255, 0, 1, 2)
$panel.TransparencyKey = $panel.BackColor
$panel.Padding = New-Object System.Windows.Forms.Padding 10, 5, 10, 5
$panel.StartPosition = 'Manual'
try {
  # 关闭抗闪烁（DoubleBuffered 是 protected，走反射）
  $panel.GetType().GetProperty('DoubleBuffered',
    [System.Reflection.BindingFlags]'Instance,NonPublic').SetValue($panel, $true, $null)
} catch {}

$panel.add_Paint({
  param($s, $e)
  # 透明背景上 ClearType 不可用，用高质量灰度抗锯齿
  $e.Graphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
  $x = [float]$s.Padding.Left
  foreach ($seg in $script:panelSegs) {
    $brush = New-Object System.Drawing.SolidBrush $seg.color
    $sz = $e.Graphics.MeasureString($seg.text, $script:panelFont)
    $e.Graphics.DrawString($seg.text, $script:panelFont, $brush, $x, [float]$s.Padding.Top)
    $x += $sz.Width
    $brush.Dispose()
  }
})

function Show-Panel([object[]]$segs) {
  $script:panelSegs = $segs
  # 按内容自适应宽度
  $g = $panel.CreateGraphics()
  $w = 0.0
  foreach ($seg in $segs) { $w += $g.MeasureString($seg.text, $script:panelFont).Width }
  $need = [int][Math]::Ceiling($w) + $panel.Padding.Left + $panel.Padding.Right
  $needH = $script:panelFont.Height + $panel.Padding.Top + $panel.Padding.Bottom + 6
  if ($needH -lt 34) { $needH = 34 }
  $g.Dispose()
  if ([Math]::Abs($panel.ClientSize.Width - $need) -gt 2 -or [Math]::Abs($panel.ClientSize.Height - $needH) -gt 2) {
    $panel.ClientSize = New-Object System.Drawing.Size $need, $needH
  }
  if (-not $script:panelEmbedded) {
    # 还没挂进任务栏：先按悬浮窗定位（右下、贴任务栏）
    if (-not $script:panelPlaced) {
      $wa = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
      $panel.Left = $wa.Right - $panel.Width - 24
      $panel.Top = $wa.Bottom - $panel.Height - 8
      $script:panelPlaced = $true
    } elseif (-not $script:panelDragging) {
      # 宽度变了以后把窗口夹回工作区，别伸出屏幕外
      $wa = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
      if ($panel.Left + $panel.Width > $wa.Right) { $panel.Left = $wa.Right - $panel.Width }
      if ($panel.Top + $panel.Height > $wa.Bottom) { $panel.Top = $wa.Bottom - $panel.Height }
      if ($panel.Left -lt $wa.Left) { $panel.Left = $wa.Left }
      if ($panel.Top -lt $wa.Top) { $panel.Top = $wa.Top }
    }
  } else {
    # 已在任务栏内：右端锚在时钟区左边，垂直居中
    # （子窗口坐标相对任务栏客户区：x 与屏幕一致，y 从任务栏顶算起）
    $panel.Left = $script:panelAnchorRight - $panel.Width
    $panel.Top = [int](($script:panelTBH - $panel.Height) / 2)
  }
  if (-not $panel.Visible) { $panel.Show() }
  $panel.Invalidate()

  # 首次显示后把面板挂进任务栏内部（失败则保持悬浮）
  if (-not $script:panelEmbedded) {
    try {
      $hTray = [TBWin]::FindWindow('Shell_TrayWnd', $null)
      if ($hTray -ne [IntPtr]::Zero) {
        $rT = New-Object TBWin+RECT
        [void][TBWin]::GetWindowRect($hTray, [ref]$rT)
        $rightEdge = $rT.Right - 12
        $hNotify = [TBWin]::FindWindowEx($hTray, [IntPtr]::Zero, 'TrayNotifyWnd', $null)
        if ($hNotify -ne [IntPtr]::Zero) {
          $rN = New-Object TBWin+RECT
          [void][TBWin]::GetWindowRect($hNotify, [ref]$rN)
          if ($rN.Left -gt $rT.Left) { $rightEdge = $rN.Left - 12 }
        }
        [void][TBWin]::SetParent($panel.Handle, $hTray)
        $panel.TopMost = $false
        $script:panelEmbedded = $true
        $script:panelAnchorRight = $rightEdge
        $script:panelTBTop = $rT.Top
        $script:panelTBH = $rT.Bottom - $rT.Top
      }
    } catch {}
  }
}
function Hide-Panel { if ($panel.Visible) { $panel.Hide() } }

# 拖动 + 双击打开主窗口；右键菜单与托盘图标共用
$panel.add_MouseDown({
  param($s, $e)
  if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Left) {
    $p = [System.Windows.Forms.Cursor]::Position
    $script:panelDragging = $true
    $script:panelDragDX = $p.X - $s.Left
    $script:panelDragDY = $p.Y - $s.Top
  }
})
$panel.add_MouseMove({
  param($s, $e)
  if ($script:panelDragging) {
    $p = [System.Windows.Forms.Cursor]::Position
    $s.Left = $p.X - $script:panelDragDX
    $s.Top = $p.Y - $script:panelDragDY
  }
})
$panel.add_MouseUp({ param($s, $e) $script:panelDragging = $false })
$panel.add_DoubleClick({ Invoke-Api '/api/show' })

function Invoke-Api([string]$path) {
  try {
    $wc = New-Object System.Net.WebClient
    $wc.Proxy = $null
    $wc.Encoding = [System.Text.Encoding]::UTF8
    $wc.Headers.Add('Content-Type', 'application/json')
    [void]$wc.UploadString("$base$path", 'POST', '{}')
  } catch {}
}

function Invoke-ApiConfig([string]$m) {
  try {
    $wc = New-Object System.Net.WebClient
    $wc.Proxy = $null
    $wc.Encoding = [System.Text.Encoding]::UTF8
    $wc.Headers.Add('Content-Type', 'application/json')
    [void]$wc.UploadString("$base/api/config", 'POST', ('{"trayMode":"' + $m + '"}'))
  } catch {}
}

# ---------------------------------------------------------------- 托盘图标
$ni = New-Object System.Windows.Forms.NotifyIcon
$ni.Text = '酷态科10号Ultra · 正在启动…'
$ni.Visible = $true

$menu = New-Object System.Windows.Forms.ContextMenuStrip
$itemOpen = $menu.Items.Add('打开窗口')
$itemOpen.add_Click({ Invoke-Api '/api/show' })
[void]$menu.Items.Add('-')

# 显示模式切换（勾选状态每个轮询周期刷新）
$mTotal = New-Object System.Windows.Forms.ToolStripMenuItem('仅显示总功率')
$mTotal.add_Click({ Invoke-ApiConfig 'total' })
[void]$menu.Items.Add($mTotal)
$mAll = New-Object System.Windows.Forms.ToolStripMenuItem('显示全部功率（图标轮流）')
$mAll.add_Click({ Invoke-ApiConfig 'all' })
[void]$menu.Items.Add($mAll)
$mPanel = New-Object System.Windows.Forms.ToolStripMenuItem('一行面板（四口+总，同时显示）')
$mPanel.add_Click({ Invoke-ApiConfig 'panel' })
[void]$menu.Items.Add($mPanel)
[void]$menu.Items.Add('-')

$itemQuit = $menu.Items.Add('退出')
$itemQuit.add_Click({
  Invoke-Api '/api/quit'
  $timer.Stop()
  $ni.Visible = $false
  try { $panel.Close() } catch {}
  [System.Windows.Forms.Application]::Exit()
})
$ni.ContextMenuStrip = $menu
$panel.ContextMenuStrip = $menu

# ---------------------------------------------------------------- 轮询
$script:fail = 0
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = $PollMs
$timer.add_Tick({
  try {
    $wc = New-Object System.Net.WebClient
    $wc.Proxy = $null
    # ⚠必须显式 UTF-8：默认按系统 ANSI(GBK) 解码，中文一旦被拆字会吞掉 JSON 的引号
    $wc.Encoding = [System.Text.Encoding]::UTF8
    try { $raw = $wc.DownloadString("$base/api/state") }
    catch { throw }                      # 网络类失败才计入 fail，连续 6 次才退出
    $s = $raw | ConvertFrom-Json
    $script:fail = 0

    # 显示模式跟随服务端配置（网页或右键菜单改了，下一秒就生效）
    $m = [string]$s.config.trayMode
    if ($m -in @('all','panel','total')) { $script:mode = $m }
    $mTotal.Checked = ($script:mode -eq 'total')
    $mAll.Checked   = ($script:mode -eq 'all')
    $mPanel.Checked = ($script:mode -eq 'panel')

    # 四口功率在 latest.ports 数组里（api/state 没有 latest.w 这个字段）
    $w = $null
    if ($null -ne $s.latest -and $null -ne $s.latest.ports) {
      $w = @{}
      foreach ($p in $s.latest.ports) { $w[[string]$p.id] = [double]$p.w }
    }

    $total = 0.0
    if ($null -ne $s.latest -and $null -ne $s.latest.total) { $total = [double]$s.latest.total }
    $state = [string]$s.link.state

    # 托盘图标：panel 模式下显示总功率，其余按各自模式
    $iconMode = if ($script:mode -eq 'all') { 'all' } else { 'total' }
    $frames = @()
    $frames += @{ label = ''; watts = $total }
    if ($iconMode -eq 'all' -and $state -eq 'connected' -and $null -ne $w) {
      $frames += @{ label = 'C1'; watts = $w['c1'] }
      $frames += @{ label = 'C2'; watts = $w['c2'] }
      $frames += @{ label = 'C3'; watts = $w['c3'] }
      $frames += @{ label = 'A';  watts = $w['a'] }
    }
    $idx = $script:frameSeq % $frames.Count
    $script:frameSeq++
    New-PowerIconFrame $frames[$idx].label $frames[$idx].watts $state | Out-Null

    # 悬浮面板：panel 模式才显示
    if ($script:mode -eq 'panel') {
      if ($state -eq 'connected' -and $null -ne $w) {
        Show-Panel @(
          @{ text = ('总 {0:0.#}W   ' -f $total);            color = $colBright }
          @{ text = ('C1 {0:0.#}W   ' -f $w['c1']);          color = $portColor['C1'] }
          @{ text = ('C2 {0:0.#}W   ' -f $w['c2']);          color = $portColor['C2'] }
          @{ text = ('C3 {0:0.#}W   ' -f $w['c3']);          color = $portColor['C3'] }
          @{ text = ('A  {0:0.#}W'    -f $w['a']);           color = $portColor['A'] }
        ) | Out-Null
      } else {
        Show-Panel @(@{ text = '酷态科10号Ultra · 连接中…'; color = $colIdle }) | Out-Null
      }
    } else {
      Hide-Panel
    }

    if ($state -eq 'connected') {
      $tip = ('总功率 {0:0.#} W' -f $total)
      if ($null -ne $w) {
        $tip += "`nC1 $([math]::Round($w['c1'],1)) W   C2 $([math]::Round($w['c2'],1)) W"
        $tip += "`nC3 $([math]::Round($w['c3'],1)) W   A  $([math]::Round($w['a'],1)) W"
      }
      if ($script:mode -eq 'all') { $tip += "`n（图标轮流显示各口功率）" }
      if ($script:mode -eq 'panel') { $tip += "`n（悬浮面板同时显示四口+总，可拖动）" }
    } elseif ($state -eq 'scanning' -or $state -eq 'connecting') {
      $tip = '酷态科10号Ultra · 正在连接充电头…'
    } else {
      $tip = '酷态科10号Ultra · ' + [string]$s.link.msg
    }
    if ($tip.Length -gt 63) { $tip = $tip.Substring(0, 63) }
    $ni.Text = $tip
  } catch {
    # 只在真正连不上服务时才累计 fail；解析类的偶发问题下个周期自然恢复
    if ($_.Exception -is [System.Net.WebException] -or $_.Exception -is [System.IO.IOException]) {
      $script:fail++
    } else {
      $script:fail = 0
    }
    # 把最近一次失败原因留在磁盘上，方便排查（比如代理、端口、权限问题）
    try {
      [IO.File]::WriteAllText("$env:TEMP\tray-last-error.txt",
        ("{0}  fail#{1}`n{2}" -f (Get-Date -Format 'HH:mm:ss'), $script:fail, $_.Exception.ToString()))
    } catch {}
    if ($script:fail -ge 6) {
      # 服务没了（正常退出或被关掉），不要留下孤儿图标/面板
      $timer.Stop()
      $ni.Visible = $false
      try { $panel.Close() } catch {}
      [System.Windows.Forms.Application]::Exit()
    }
  }
})
$timer.Start()
[System.Windows.Forms.Application]::Run()

try { $ni.Visible = $false; $ni.Dispose(); $menu.Dispose(); $bmp.Dispose(); $panel.Dispose() } catch {}
