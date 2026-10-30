# BCI 版本化映射接口设计

本接口采用每次获取的只读不可变快照，解决旧 DMA mmap 缺少消费游标、设备寿命和覆盖同步的问题。它不是实时零复制 ring；实时八通道帧由 C bci 工具输出。内核旧 read 仍按连续 signed32 nV 保持兼容。

## UAPI v1

- 新 ioctl 13 `XTOS_BCI_SNAPSHOT_ACQUIRE`，in/out 固定64位对齐结构：version=1、flags=0、requested_bytes>0、mapping_bytes、payload_bytes、cursor、token、channels、sample_rate_hz。reserved=0，严格拒绝未知版本/flags/未对齐大小。
- 新 ioctl 14 `XTOS_BCI_SNAPSHOT_CONSUME`：version=1、flags=0、token、cursor、bytes。只允许整份已获取 payload，游标需仍等于 ring read_pos；source 覆盖后返回 EOVERFLOW，不消费另一份数据。
- 新 ioctl 15 `XTOS_BCI_SNAPSHOT_RELEASE`：释放 session 持有引用；现有 VMA 引用继续有效。释放后可再次 acquire，token 不重用。
- snapshot header：magic/version/header_size/payload_bytes/channels/rate/unit=nanovolt/sample_size=4/cursor/token。小端/原生序约束明确，alignment/layout 编译验证，zeroed padding。

获取时从 ring 的受锁读位置复制到新的 page-rounded vmalloc_user 区域，按整通道帧（channels*4）截断上限64KiB；不足一帧返回 EAGAIN。未 consume 前不移动 ring read_pos。每 FD 同时最多一快照（EBUSY），mmap 必须 offset0、exact mapping_bytes、只读MAP_SHARED，VM_MAYWRITE清除禁止 mprotect 写。VMA open/close refcount 对象；不持有 driver 私有数据或DMA映射。

consume 在同一 device operation mutex 下检查 connected 与 active token，再持 ring spinlock比对游标并移动；检测覆盖后仍可读取旧 snapshot 但不能消费。普通 read 与 outstanding snapshot混用时返回 EBUSY避免无声改变游标。解绑之后现有 snapshot VMA 的内存仍有效；所有 device ioctl明确 ENODEV，FD release不碰已断开的transport。映射对象释放不需要设备回调，内存最后一个引用消失才 vfree。

SDK新增 acquire/consume/release，返回 metadata与const payload；旧 get_mmap_buffer 不再猜容量，明确 EOPNOTSUPP，建议使用新接口。测试必须包括 mmap跨close/unbind、mprotect/offset/长度拒绝、read混用、cursor覆盖、consume重放、页填零和资源计数。
