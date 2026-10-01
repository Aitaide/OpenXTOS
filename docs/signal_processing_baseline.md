# 阶段3B：确定性信号处理基线

## 本轮范围

本轮实现不依赖第三方库的C信号处理基础：

- 采样时间戳检查
- 等采样率时间戳校正
- radix-2 FFT及单边平方幅值谱
- 可配置频率和Q值的二阶IIR陷波器
- 可配置截止频率的四阶Butterworth带通基线
- SSVEP目标频率及谐波功率、邻频噪声和SNR特征
- P300 ERP基线校正、正向峰值、峰值延迟、窗口能量和z分数检测

当前没有实现SSVEP分类器、P300分类器或AI推理。

## API and rules

`xtos_signal_validate_timestamps`检查时间戳严格递增、采样间隔和异常计数。返回`0`表示无异常，返回`1`表示发现异常，参数错误返回`-1`并设置`EINVAL`。

`xtos_signal_correct_timestamps`按`round(1e9 / sample_rate_hz)`生成等间隔纳秒时间戳。它是显式校正工具，不会悄悄修改原始数据。

`xtos_signal_fft_power`要求输入长度为2的幂，输出0到`N/2`的单边平方幅值。输出没有额外的单边能量归一化，调用者必须按同一约定比较结果。

`xtos_notch_init`使用二阶RBJ notch结构，采样率、陷波频率和Q值由调用者提供；50Hz和60Hz不是硬编码默认频率。

`xtos_bandpass_init`创建两个二阶Butterworth节：低截止频率的高通节串联高截止频率的低通节。该实现是研究用途的四阶基线，不是线性相位滤波器，也没有医疗用途认证。截止频率必须满足`0 < low_cut_hz < high_cut_hz < sample_rate_hz / 2`。

`xtos_signal_ssvep_feature`把目标频率映射到最近FFT频点，累加指定数量谐波的功率，并在每个目标频点两侧的邻频中排除所有目标谐波后计算平均噪声功率。输出线性SNR和dB SNR。该实现不做频率插值、分类或刺激协议判断。

`xtos_signal_p300_feature`把输入前`baseline_samples`个样本作为刺激前基线，计算基线均值和总体标准差，再在相对刺激时间的`window_start_ms`到`window_end_ms`窗口内寻找最大正向基线校正峰值。输出峰值幅度、相对刺激延迟、窗口能量和z分数；z分数达到`detection_z_threshold`时将`detected`置为1。输入不会被修改，标准差为零、窗口越界、非有限值和参数错误会返回错误。它是ERP研究基线，不是P300分类器，也不判断刺激协议或试次统计。

## 构建和测试

```sh
cd sdk/libxtos-signal
make clean && make test && make
```

实际结果：

```text
PASS assertions=132
Python reference: 1 test OK
```

测试覆盖：

- 250Hz等间隔时间戳
- 采样间隔异常
- 非单调时间戳
- 256Hz时间戳校正
- 8点单位脉冲FFT
- 8点单周期正弦FFT
- 非2幂FFT长度和输出容量错误
- 50Hz正弦信号陷波衰减
- 陷波频率/Q值非法参数
- 1Hz、10Hz和80Hz带通频率响应
- 带通截止频率非法参数
- 带通原地处理与独立输入输出结果一致
- P300基线、正向峰值、延迟、z分数和窗口边界
- 10Hz基频和20Hz二次谐波SSVEP特征
- SSVEP参数错误和谐波边界

## 产物

- `sdk/libxtos-signal/include/xtos/signal.h`
- `sdk/libxtos-signal/src/signal.c`
- `sdk/libxtos-signal/tests/test_signal.c`
- `sdk/libxtos-signal/tests/test_signal_reference.py`
- `tools/xtos-signal-benchmark/`

## 边界

这些测试是固定输入的算法单元测试，不是EEG硬件测试，也不代表滤波器或ERP检测满足医疗用途。带通基线采用因果IIR串联结构，存在启动瞬态和相位响应；SSVEP特征只验证频点功率和SNR计算，不代表刺激识别准确率；P300基线只验证单窗口正向峰值和阈值行为，不代表跨试次分类准确率。处理基准`xtos-signal-baseline-v2`同时记录P300特征路径的延迟、CPU和峰值RSS，性能数字仍受主机和系统负载影响。
