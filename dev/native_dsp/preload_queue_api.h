#pragma once
#include <cstdint>

// Shared C ABI between the native MIDI consumer and the independent I/O TU.
// Definitions remain in preload_queue.cpp. This boundary is intentionally one
// header so argument count/order/return/cdecl cannot silently diverge between
// independently compiled callers and providers. No Python or audio callback.
struct FontPreloadQueueTarget;
struct FontPreloadQueueResult;
struct FontPreloadRoute;
struct FontPreloadAttempt;
extern "C" {
__declspec(dllexport) unsigned __cdecl font_preload_plan_version();
__declspec(dllexport) unsigned __cdecl font_preload_plan_live_count();
__declspec(dllexport) unsigned __cdecl font_preload_plan_cache_version();
__declspec(dllexport) unsigned __cdecl font_preload_plan_empty_version();
__declspec(dllexport) unsigned __cdecl font_preload_plan_address_domain_version();
__declspec(dllexport) unsigned __cdecl font_preload_plan_request_version();
__declspec(dllexport) void* __cdecl font_preload_plan_create(
    const FontPreloadQueueTarget*,unsigned,const unsigned char*,const FontPreloadRoute*,unsigned,
    const FontPreloadAttempt*,unsigned,const unsigned char*,std::uint64_t,std::uint64_t,unsigned,unsigned,
    unsigned,void*,void*,void*,void*);
__declspec(dllexport) int __cdecl font_preload_plan_qualify(
    void*,std::uint64_t,std::uint64_t,unsigned,unsigned,unsigned,unsigned,unsigned,unsigned);
__declspec(dllexport) int __cdecl font_preload_plan_before_note(
    void*,std::uint64_t,std::uint64_t,unsigned,unsigned,unsigned,unsigned,unsigned,unsigned,std::uint64_t*);
__declspec(dllexport) int __cdecl font_preload_plan_status(void*,std::uint64_t*);
__declspec(dllexport) int __cdecl font_preload_plan_io_status(void*,std::uint64_t*);
__declspec(dllexport) int __cdecl font_preload_plan_io_result(void*,unsigned,FontPreloadQueueResult*);
__declspec(dllexport) int __cdecl font_preload_plan_cache(void*,unsigned char*,unsigned,unsigned char*,unsigned);
__declspec(dllexport) unsigned __cdecl font_preload_plan_retain(void*);
__declspec(dllexport) int __cdecl font_preload_plan_acquire(void*,std::uint64_t,std::uint64_t,unsigned,unsigned);
__declspec(dllexport) unsigned __cdecl font_preload_plan_release(void*);
__declspec(dllexport) int __cdecl font_preload_plan_cancel(void*);
__declspec(dllexport) int __cdecl font_preload_plan_join(void*,unsigned);
__declspec(dllexport) int __cdecl font_preload_plan_destroy(void*);
}
