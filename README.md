<div align="center">

# astrbot_plugin_bye

识别不友善以及不欢迎bot的群聊，让bot主动退群，保护bot身心健康，从源头上节省Tokens。 


[![License](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-4.0%2B-orange.svg)](https://github.com/Soulter/AstrBot)
[![GitHub](https://img.shields.io/badge/作者-largefox-blue)](#)

</div>


## 📦 安装和配置

在 AstrBot 插件市场搜索 **astrbot_plugin_bye**，点击安装即可生效启用。
前往 AstrBot 插件配置面板即可进行设置。

---

## 🤝 这是干嘛用的？

当bot在群里遭遇频繁禁言、群友发言对bot有明显敌意、排斥甚至辱骂bot时，插件会帮助bot识别并退群，保证群聊环境的清爽，确保bot身心健康，从源头上节省Tokens。

---

## ⌨️ 命令大全

| 命令 | 干什么用的 |
|------|-----------|
| `/bye` | （如前缀设置为其他的，则为 `[前缀]bye`）最原生的默认指令。在群聊发送该指令后，bot抛出一个告别短语并随后退群。 |
| *自定义文本* | 你可以在控制面板设定如 `你褪裙吧`，不带前缀发群里即可产生等同 `/bye` 的强制效果。 |


---

## 📋 配置归类一览表

| 分类配置块                 | 你可以在里面调节什么                                               |
| ------------------- | ------------------------------------------------------ |
| **【全局设置】**          | `白名单列表`与 `告别语`设定               |
| **【禁言退群】**       | `禁言次数上限` / `禁言时长上限` / `中途提前解禁后的判定方式` / `名片提示的倒计时触发线` |
| **【LLM语义判定退群】**     | `LLM判定关键词列表` / `指定哪一个模型ID来进行判断` / `被骂最高容忍次数`          |
| **【指令退群】**       | `/bye 功能总闸口开关` / `自定义中文退群指令词` |

---

## 👥 交流与反馈

- 🌟 如果这个插件成功保护了你的bot的身心健康，请毫不吝啬地在 Github 上点亮你的 Star，让更多人看到！
- 💡 或者随时向原仓库的作者反馈好玩的脑洞与功能建议。