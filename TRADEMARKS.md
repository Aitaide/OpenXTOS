# XTOS 来源标注与品牌使用说明

# XTOS Attribution and Brand Use

本说明区分开源许可义务、推荐的来源标注和品牌授权。它不改变、不增加、不收回 GPL-2.0、Apache-2.0 或第三方组件许可证授予的权利。仅营利性使用不产生本说明之外的新收费、审批或强制 Logo 展示义务。具体义务以所采用版本及组件的许可证为准。

This document separates software-license obligations, recommended attribution, and permission to use project brands. It does not change, add to, or revoke rights granted under GPL-2.0, Apache-2.0, or a third-party component license. Commercial use alone does not create an additional payment, approval, or mandatory logo-display requirement. The license of the adopted version and component controls.

## 1. 开源许可义务 / Software-license obligations

- 再分发源码、镜像、模块、SDK 或工具时，按适用许可证保留版权、许可、无担保、署名及修改通知。
- GPL-2.0 二进制分发须按其第 3 条满足对应源码提供义务。Logo 或来源标注不能代替这些义务。
- Apache-2.0 发行物包含适用 NOTICE 时，按其第 4(d) 条保留可读的署名内容；可放在随附 NOTICE、源码/文档或通常展示第三方声明的位置。NOTICE 不修改许可证。
- 仅通过正常系统调用使用 Linux，或只使用某个 SDK，不应因此把独立应用宣传为 XTOS 的官方发行版。

- When redistributing source, images, modules, SDKs, or tools, preserve the copyright, license, warranty, attribution, and modification notices required by the applicable license.
- GPL-2.0 binary distributions must meet the corresponding-source requirements of section 3. A logo or attribution is not a substitute.
- Where Apache-2.0 section 4(d) applies to NOTICE material, include its readable attribution notices in an accompanying NOTICE, source/documentation, or a display where third-party notices normally appear. NOTICE does not modify the license.
- Normal Linux syscall use or use of an individual SDK does not make an independent application an official XTOS distribution.

## 2. 推荐来源标注 / Recommended attribution

对于整合、修改或再分发 OpenXTOS 的产品、设备、系统或服务，推荐在“关于、系统信息、开源软件声明”或等效位置，以及随附文档中，清楚标注以下信息：

For a product, device, system, or service that integrates, modifies, or redistributes OpenXTOS, clear attribution is recommended in an About, system-information, open-source notices, or equivalent screen and the accompanying documentation:

- 项目名称：OpenXTOS / XTOS。
- 实际采用的上游版本和已知源码提交；不可只显示自己产品的版本。
- 官网：https://xtos.cn。
- 整合范围：完整系统或实际使用的组件，例如 libxtos-rt。
- 修改情况，以及实际提供产品、支持和保证的厂商名称。

- Project name: OpenXTOS / XTOS.
- The actual upstream version and known source commit, separate from the downstream product version.
- Website: https://xtos.cn.
- Integration scope: the full system or the actual component, such as libxtos-rt.
- Modification status and the vendor that provides the product, support, and any warranty.

推荐位置应正常可访问、可阅读，不应使用无法辨认的文字或颜色。无图形界面的设备可在管理界面、版本命令、设备说明书或随附开源声明中提供信息。只使用某个 SDK 时，应写“包含 libxtos-rt”，而不是“基于完整 XTOS 系统”。这是推荐做法，不是新增的软件许可条件。

Recommended placement should be accessible and readable in normal use. A headless device may use its management interface, version command, manual, or accompanying open-source notices. For SDK-only integration, state "includes libxtos-rt" rather than "based on the complete XTOS system". These are recommendations, not additional software-license conditions.

## 3. 名称与 Logo / Names and logos

XTOS、OpenXTOS 的名称和官方 Logo 用于识别项目。代码许可证不自动授予把这些标识用于自己产品名称、主要品牌、认证或联合品牌的权利。不得造成该产品由 XTOS 官方提供、认证、赞助或背书的误解。

The XTOS/OpenXTOS names and official logos identify the project. Software licenses do not automatically grant permission to use them as a downstream product name, primary brand, certification mark, or co-brand. They must not imply that the project provides, certifies, sponsors, or endorses the downstream product.

准确、适度地以文字说明“基于 OpenXTOS”或“包含 libxtos-rt”不需要申请本说明额外设置的批准；适用法律允许的合理描述性使用不受本说明限制。建议附上实际上游版本和官网。不得把修改版称为官方发行版。

Accurate, proportionate textual descriptions such as "based on OpenXTOS" or "includes libxtos-rt" do not require an additional approval imposed by this document. Descriptive use permitted by applicable law is not restricted. Identify the actual upstream version and website, and do not call a modified downstream version an official release.

除适用法律允许的使用或另有授权外，使用官方 Logo 作为产品来源标识、认证标识或联合品牌，应先获得书面授权。授权应明确允许的使用场景、Logo 文件、展示位置、尺寸、版本标注、期限和撤回条件。授权不是代码使用许可，也不免除开源许可证义务。

Except for uses permitted by applicable law or otherwise authorized, obtain written permission before using an official logo as a product-origin, certification, or co-branding mark. Permission should specify the context, logo asset, placement, size, version attribution, term, and withdrawal conditions. Brand permission is not permission to use the code and does not remove software-license obligations.

获得授权后，应使用提供的官方 Logo，不得拉伸、变形、裁切关键图形、去掉标识文字或擅自改变颜色；修改版产品须同时标明自己的厂商身份和修改情况。普通来源标注不代表官方认证，也不代表官方提供支持或担保。

After authorization, use the supplied official logo without distortion, removal of identifying text, or unapproved color changes. Modified products should also identify their own vendor and modifications. Attribution is not official certification and does not imply project support or warranty.

## 4. 标注示例 / Attribution examples

将占位内容替换为实际信息；仅采用某个组件时使用组件示例。

Replace placeholders with actual information; use the component example for component-only integration.

完整系统 / Full system:

    本产品基于 OpenXTOS [上游版本] 开发。
    整合范围：[完整系统及组件清单]。
    修改情况：[修改摘要或未修改]。
    项目官网：https://xtos.cn
    产品提供方：[厂商名称]。
    本产品不代表 XTOS 官方发行、认证或背书。

    This product is based on OpenXTOS [upstream version].
    Integration: [system and component list].
    Modifications: [summary or none].
    Project website: https://xtos.cn
    Product vendor: [vendor name].
    This is not an official XTOS release, certification, or endorsement.

组件 / Component:

    本产品包含 OpenXTOS 的 libxtos-rt [库版本]。
    上游版本/提交：[实际采用的信息]。
    项目官网：https://xtos.cn
    修改情况：[修改摘要或未修改]。

    This product includes OpenXTOS libxtos-rt [library version].
    Upstream version/commit: [actual adopted source].
    Project website: https://xtos.cn
    Modifications: [summary or none].

## 5. 随附文件与联系 / Distribution files and contact

源码仓库和发行包应按适用许可随附 LICENSE、LICENSE-NOTICE、适用 NOTICE、第三方许可证与通知，并记录 VERSION、源码提交、组件清单和修改。推荐随附本说明作为品牌说明，不把它当作限制 GPL/Apache 权利的附加许可证。

Distribute LICENSE, LICENSE-NOTICE, applicable NOTICE material, and third-party licenses/notices as required, and record VERSION, source commit, components, and changes. Including this brand guide is recommended; it is not an additional license restricting GPL/Apache rights.

本说明不对已发布版本的许可追溯增加限制。需要强制 Logo 展示、联合品牌、官方认证或特定商业合作条件时，必须以具体书面协议确定，并核对相关权利范围；不能仅凭官网公告改变既有软件许可。

This guide does not retroactively add restrictions to released software. Mandatory logo placement, co-branding, official certification, or specific commercial arrangements require a separate written agreement and verification of the relevant rights; a website notice alone does not change an existing software license.

维护机构 / Maintainer: 嘉兴市艾泰德集团有限公司 / Jiaxing ITADD Group Co., Ltd.

官网 / Website: https://xtos.cn

来源仓库 / Source: https://github.com/Aitaide/OpenXTOS

品牌事项联系 / Brand enquiries: org@dcom.si

参考 / References:

- Apache-2.0, sections 4 and 6: https://www.apache.org/licenses/LICENSE-2.0
- GPL-2.0, sections 1, 2, 3 and 6: https://www.gnu.org/licenses/old-licenses/gpl-2.0.en.html

中英文用于方便阅读。如表述存在差异，不得解释为减少适用开源许可证授予的权利。具体分发或品牌合作仍需结合所用组件和实际协议审查。

Both languages are provided for readability. A discrepancy must not be interpreted to reduce rights granted by an applicable open-source license. Assess the actual components and agreements for a particular distribution or brand arrangement.
