import argostranslate.package
import argostranslate.translate

# def install_package(from_code, to_code):
#     """安装指定方向的语言包（如果尚未安装）"""
#     # 先检查是否已安装，避免重复下载
#     installed = argostranslate.package.get_installed_packages()
#     for pkg in installed:
#         if pkg.from_code == from_code and pkg.to_code == to_code:
#             print(f"已安装 {from_code} -> {to_code}，跳过下载")
#             return
    
#     print(f"正在下载并安装 {from_code} -> {to_code} ...")
#     argostranslate.package.update_package_index()
#     available_packages = argostranslate.package.get_available_packages()
#     package_to_install = next(
#         filter(
#             lambda x: x.from_code == from_code and x.to_code == to_code,
#             available_packages
#         )
#     )
#     argostranslate.package.install_from_path(package_to_install.download())
#     print(f"安装完成: {from_code} -> {to_code}")

# # 安装两个方向的包
# install_package("en", "zh")  # 英译中
# install_package("zh", "en")  # 中译英

# 翻译示例
en_text = "Hello, how are you?fuck"
zh_result = argostranslate.translate.translate(en_text, "en", "zh")
print(f"\n英译中: {en_text} -> {zh_result}")

zh_text = "你好，最近怎么样？"
en_result = argostranslate.translate.translate(zh_text, "zh", "en")
print(f"中译英: {zh_text} -> {en_result}")