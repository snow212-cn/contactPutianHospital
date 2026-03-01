# contactPutianHospital

## 项目说明

用于自动化访问网页并发送预设咨询消息及联系方式，项目包括：

- 网址采集脚本：[catch.py](catchad/catch.py)
- 主流程脚本：[main.py](main.py)
- 调度脚本：[scheduler.py](scheduler.py)

## 快速开始

1. 安装依赖：

```bash
pip install -r requirements.txt
```

2. 复制示例配置并填写你自己的本地配置：

```bash
copy config.example.yaml config.yaml
```

3. 按需修改 [config.yaml](config.yaml) 后运行：

```bash
python main.py
```

## 配置文件

- 示例配置文件：[`config.example.yaml`](config.example.yaml)
- 本地实际配置文件：[`config.yaml`](config.yaml)（已在 `.gitignore` 中忽略）

## 免责声明

本项目内容仅用于技术研究与测试，请遵守当地法律法规与平台规则。使用者应对自身行为及后果负责。
