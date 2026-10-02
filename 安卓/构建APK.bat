@echo off
chcp 65001 >nul
setlocal

REM ============================================================================
REM  酷态科10号Ultra · 安卓版 一键构建
REM  产物：<本目录>\out\cuktech10ultra-2.0.0.apk
REM
REM  不依赖 Gradle / Android Studio：javac → d8 → aapt2 → zipalign → apksigner。
REM  需要两样东西，可用环境变量覆盖，也可直接改下面两行默认值：
REM    JAVA_HOME    JDK 17（或 11）
REM    ANDROID_HOME Android SDK（含 platforms;android-34 与 build-tools;34.0.0）
REM ============================================================================

if "%JAVA_HOME%"==""    set "JAVA_HOME=E:\workbuddy\.android-toolchain\jdk\jdk-17.0.20.1+1"
if "%ANDROID_HOME%"=="" set "ANDROID_HOME=E:\workbuddy\.android-toolchain\android-sdk"

set "SRC=%~dp0app\src\main"
set "OUT=%~dp0out"
set "BUILD=%~dp0build"
set "PLATFORM=%ANDROID_HOME%\platforms\android-34"
set "BT=%ANDROID_HOME%\build-tools\34.0.0"
set "JAVAC=%JAVA_HOME%\bin\javac.exe"
set "JAR=%JAVA_HOME%\bin\jar.exe"
set "KEYTOOL=%JAVA_HOME%\bin\keytool.exe"
set "AAPT2=%BT%\aapt2.exe"
set "ZIPALIGN=%BT%\zipalign.exe"
set "D8=%BT%\lib\d8.jar"
set "APKSIGNER=%BT%\lib\apksigner.jar"
set "KEYSTORE=%~dp0cuktech.jks"
rem 产品版本（带 -H 后缀 = 内置鸿蒙 UI 主题，界面上一键切换）
set "VERSION_NAME=2.0.0.1-H"
set "VERSION_CODE=3"

if not exist "%JAVAC%"    echo [X] 找不到 javac：%JAVAC%        & goto :fail
if not exist "%AAPT2%"    echo [X] 找不到 aapt2：%AAPT2%（请先安装 platforms;android-34 与 build-tools;34.0.0） & goto :fail

echo [1/6] 清理并准备输出目录
if exist "%BUILD%" rmdir /s /q "%BUILD%"
if not exist "%OUT%" mkdir "%OUT%"
mkdir "%BUILD%\classes"
mkdir "%BUILD%\dex"
mkdir "%BUILD%\gen"

echo [2/6] 编译 Java 源码
dir /s /b "%SRC%\java\*.java" > "%BUILD%\sources.txt"
"%JAVAC%" -encoding UTF-8 -source 8 -target 8 -nowarn ^
    -bootclasspath "%PLATFORM%\android.jar" ^
    -classpath "%PLATFORM%\android.jar" ^
    -d "%BUILD%\classes" @"%BUILD%\sources.txt"
if errorlevel 1 echo [X] javac 失败 & goto :fail

echo [3/6] class → dex
REM d8.jar 没有 Main-Class，必须显式给主类名
"%JAVA_HOME%\bin\java.exe" -cp "%D8%" com.android.tools.r8.D8 --lib "%PLATFORM%\android.jar" --min-api 26 ^
    --output "%BUILD%\dex" "%BUILD%\classes\com\histion\cuktech\*.class"
if errorlevel 1 echo [X] d8 失败 & goto :fail

echo [4/6] 编译并链接资源
"%AAPT2%" compile --dir "%SRC%\res" -o "%BUILD%\res.zip"
if errorlevel 1 echo [X] aapt2 compile 失败 & goto :fail
"%AAPT2%" link -I "%PLATFORM%\android.jar" ^
    --manifest "%SRC%\AndroidManifest.xml" ^
    -A "%SRC%\assets" ^
    --java "%BUILD%\gen" ^
    --min-sdk-version 26 --target-sdk-version 34 ^
    --version-code %VERSION_CODE% --version-name %VERSION_NAME% ^
    -o "%BUILD%\app.apk" "%BUILD%\res.zip"
if errorlevel 1 echo [X] aapt2 link 失败 & goto :fail

echo [5/6] 塞入 dex 并对齐
pushd "%BUILD%\dex"
"%JAR%" uvf "%BUILD%\app.apk" classes.dex
popd
if errorlevel 1 echo [X] 打包 dex 失败 & goto :fail
"%ZIPALIGN%" -f 4 "%BUILD%\app.apk" "%BUILD%\app-aligned.apk"
if errorlevel 1 echo [X] zipalign 失败 & goto :fail

echo [6/6] 签名
if not exist "%KEYSTORE%" (
    echo      首次构建，生成调试签名 cuktech.jks
    "%KEYTOOL%" -genkeypair -v -keystore "%KEYSTORE%" -alias cuktech -keyalg RSA -keysize 2048 ^
        -validity 10950 -storepass cuktech -keypass cuktech ^
        -dname "CN=Histion, OU=cuktech, O=cuktech10ultra, L=, S=, C=CN" >nul
)
"%JAVA_HOME%\bin\java.exe" -jar "%APKSIGNER%" sign ^
    --v1-signing-enabled true --v2-signing-enabled true --v3-signing-enabled true ^
    --ks "%KEYSTORE%" --ks-key-alias cuktech ^
    --ks-pass pass:cuktech --key-pass pass:cuktech ^
    --out "%OUT%\cuktech10ultra-%VERSION_NAME%.apk" "%BUILD%\app-aligned.apk"
if errorlevel 1 echo [X] 签名失败 & goto :fail

echo.
echo [√] 构建完成：%OUT%\cuktech10ultra-%VERSION_NAME%.apk
for %%F in ("%OUT%\cuktech10ultra-%VERSION_NAME%.apk") do echo     大小：%%~zF 字节
goto :end

:fail
echo.
echo [X] 构建失败，详见上面的错误信息。
exit /b 1

:end
endlocal
pause
