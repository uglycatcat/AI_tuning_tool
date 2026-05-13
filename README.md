# API_socket
**此项目实现了基于AI的PID调参并提供了前端交互和串口调参接口**
*（当前只实现前端虚拟PID借助AI调参）*

# 快速上手
## 配置环境
1. 配置conda(python环境管理工具)
```
mkdir -p ~/miniconda3
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O ~/miniconda3/miniconda.sh
bash ~/miniconda3/miniconda.sh -b -u -p ~/miniconda3
rm ~/miniconda3/miniconda.sh
~/miniconda3/bin/conda init --all
source ~/.bashrc
```
2. 创建这个项目的python_env
```
conda env create -f environment.yml
```
3. config
## 启动！
```
conda activate ai_env
python -m web_tool
```
## 手动调参
启动->
## ai调参
勾选{接入tuning tool}->重启->开始

# 1. 项目框架
在前端搭建了可交互页面，产生了虚拟的PID（仿照物理世界的根据位置差距PID速度），然后提供了多种参数，之后通过采样和组装prompt传递给ai，得到结果解析出新版的PID参数然后同步给虚拟的PID，重复这个过程

# 2. tuning_tool
项目的核心部分，负责解析来自PID端的参考数据并组装prompt发起request和解析response

# 3. web_tool
项目的交互核心，负责展现PID当前的效果，配置参数，帮助开发者了解当前进度和情况

# 4. 串口协议


# Issues
1. 接入tuning模式下重启或切换模式，可能有正在飞行的prompt等待request，收到request后右侧的文本框出现内容并且pid被修改。所以tuning模式下重启后可以等待一段时间（最多40s），或者直接重新启动程序。