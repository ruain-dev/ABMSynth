// ABMSynth MIDI input endpoint for legacy WinMM hosts.
//
// A DAW sees this as an output device named "ABMSynth MIDI In". Every
// MIDI 1.0 byte sequence written to that device is framed and sent to the
// ABMSynth process over a local named pipe. This is intentionally a
// user-mode installable MIDI driver: no kernel driver or test-signing mode is
// needed for the one-way DAW -> ABMSynth use case.

#define NOMINMAX
#include <windows.h>
#include <mmsystem.h>
#include <mmddk.h>

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <vector>
#include <array>
#include <new>
#include <cwchar>

namespace
{
#ifdef ABMSYNTH_TRACE_SMOKE
// Isolated, non-installed build used by the real-driver pipe/mapping audit.
constexpr wchar_t kPipeName[] = L"\\\\.\\pipe\\ABMSynthMidiTraceSmoke";
constexpr wchar_t kTraceMappingName[] = L"Local\\ABMSynthMidiTraceSmokeV1";
#else
constexpr wchar_t kPipeName[] = L"\\\\.\\pipe\\ABMSynthMidiIn";
constexpr wchar_t kTraceMappingName[] = L"Local\\ABMSynthMidiTraceV1";
#endif
constexpr DWORD kFrameMagic = 0x3144494d; // "MID1" in little-endian memory.
constexpr DWORD kTraceFrameMagic = 0x3244494d; // "MID2": frame also carries a trace sequence.
constexpr DWORD kTimedFrameMagic = 0x3344494d; // MID3: always-on admission clock, not debug-only.
constexpr DWORD kMaxPayloadBytes = 1024 * 1024;
constexpr DWORD kTraceMagic = 0x31524441; // "ADR1"
constexpr DWORD kTraceCapacity = 4096;

struct FrameHeader
{
    DWORD magic;
    DWORD length;
};

struct TraceFrameHeader
{
    DWORD magic;
    DWORD length;
    LONGLONG sequence;
};

// Fixed little-endian wire ABI, identical in x86/x64. QPC is the same boot
// clock across processes (Microsoft high-resolution timestamp guidance).
struct TimedFrameHeader
{
    DWORD magic, length;
    ULONGLONG ingress, frequency, epoch, sequence;
    LONGLONG traceSequence;
    ULONGLONG losses;
    DWORD processId, flags;
};
static_assert(sizeof(TimedFrameHeader) == 64, "MID3 wire header changed");

// Shared with dev/midi_input_runtime.py. The driver never writes a file or
// waits for the diagnostic consumer from a DAW MIDI callback.
struct TraceEntry
{
    alignas(8) volatile LONG64 committedSequence;
    LONGLONG counter;
    DWORD processId;
    DWORD threadId;
    DWORD length;
    DWORD outcome;
    DWORD error;
    DWORD headerBytes;
    DWORD payloadBytes;
    BYTE prefix[8];
};

struct TraceMapping
{
    DWORD magic;
    DWORD version;
    DWORD capacity;
    volatile LONG enabled;
    alignas(8) volatile LONG64 nextSequence;
    TraceEntry entries[kTraceCapacity];
};

static_assert(sizeof(TraceFrameHeader) == 16, "MID2 wire header changed");
static_assert(sizeof(TraceEntry) == 56, "TraceEntry ABI changed");
static_assert(sizeof(TraceMapping) == 24 + kTraceCapacity * 56, "TraceMapping ABI changed");
static_assert(offsetof(TraceEntry, prefix) == 44, "TraceEntry field layout changed");
static_assert(offsetof(TraceMapping, entries) == 24, "TraceMapping field layout changed");

enum TraceOutcome : DWORD
{
    kDelivered = 1,
    kNoPipe = 2,
    kHeaderFailure = 3,
    kPayloadFailure = 4,
};

struct Instance
{
    unsigned port{};
    MIDIOPENDESC openDesc{};
    DWORD callbackFlags{};
    volatile LONG pending{};
    volatile LONG failed{};
    volatile LONG reconnectable{};
    volatile LONG resetting{};
};

SRWLOCK gPipeLock = SRWLOCK_INIT;
HANDLE gPipe = INVALID_HANDLE_VALUE;
volatile LONG gPipeConnected = 0;
HANDLE gTraceMappingHandle = nullptr;
TraceMapping* gTraceMapping = nullptr;
ULONGLONG gProducerEpoch = 0, gClockFrequency = 0, gTransportSequence = 0;
alignas(8) volatile LONG64 gTransportLosses = 0;
struct OriginHealth {
    DWORD magic,version,pid,reserved;
    ULONGLONG epoch,frequency;
    alignas(8) volatile LONG64 losses;
};
static_assert(sizeof(OriginHealth)==40,"origin health ABI changed");
// Additive tail: the original40-byte health ABI and MID3 header stay intact.
// Only failures copy at most32 bytes. No allocation, file I/O or wait occurs
// here. A contended detail writer may omit detail, never the loss counter.
constexpr DWORD kLossCapacity=128;
struct LossEntry {
    alignas(8) volatile LONG64 committed;
    LONGLONG ingress;
    DWORD length,reason,error,reserved;
    BYTE prefix[32];
};
struct OriginLossMapping {
    OriginHealth health;
    DWORD detailVersion,capacity;
    LossEntry entries[kLossCapacity];
};
static_assert(sizeof(LossEntry)==64,"loss detail ABI changed");
static_assert(offsetof(OriginLossMapping,entries)==48,"loss mapping ABI changed");
SRWLOCK gLossDetailLock=SRWLOCK_INIT;
HANDLE gOriginMapping=nullptr;
OriginHealth* gOriginHealth=nullptr;
void RecordTransportLoss(const BYTE* data,DWORD length,DWORD reason,DWORD error,LONGLONG ingress){
    const LONG64 loss=InterlockedIncrement64(&gTransportLosses);
    if(!gOriginHealth)return;
    if(TryAcquireSRWLockExclusive(&gLossDetailLock)){
        auto* mapping=reinterpret_cast<OriginLossMapping*>(gOriginHealth);
        auto& entry=mapping->entries[(loss-1)%kLossCapacity];
        InterlockedExchange64(&entry.committed,-loss);
        entry.ingress=ingress;entry.length=length;entry.reason=reason;entry.error=error;entry.reserved=0;
        std::memset(entry.prefix,0,sizeof(entry.prefix));
        if(data)std::memcpy(entry.prefix,data,std::min<DWORD>(length,sizeof(entry.prefix)));
        InterlockedExchange64(&entry.committed,loss);
        ReleaseSRWLockExclusive(&gLossDetailLock);
    }
    InterlockedIncrement64(&gOriginHealth->losses);
}
void CloseOriginHealth(){
    if(gOriginHealth)UnmapViewOfFile(gOriginHealth);
    if(gOriginMapping)CloseHandle(gOriginMapping);
    gOriginHealth=nullptr;gOriginMapping=nullptr;
}
void CreateOriginHealth(){
    wchar_t name[128]{};
    swprintf_s(name,L"Local\\ABMSynthMidiOriginV1-%lu-%llu",GetCurrentProcessId(),gProducerEpoch);
    gOriginMapping=CreateFileMappingW(INVALID_HANDLE_VALUE,nullptr,PAGE_READWRITE,0,sizeof(OriginLossMapping),name);
    if(!gOriginMapping)return;
    // Existing/colliding object is not our producer epoch; fail closed.
    if(GetLastError()==ERROR_ALREADY_EXISTS){CloseOriginHealth();return;}
    gOriginHealth=static_cast<OriginHealth*>(MapViewOfFile(gOriginMapping,FILE_MAP_ALL_ACCESS,0,0,sizeof(OriginLossMapping)));
    if(!gOriginHealth){CloseOriginHealth();return;}
    *gOriginHealth={0x314f4d41,1,GetCurrentProcessId(),sizeof(OriginLossMapping),gProducerEpoch,gClockFrequency,0};
    auto* details=reinterpret_cast<OriginLossMapping*>(gOriginHealth);
    details->detailVersion=1;details->capacity=kLossCapacity;
}

LONGLONG ReserveTraceSequenceLocked()
{
    if (!gTraceMapping)
    {
        HANDLE mapping = OpenFileMappingW(FILE_MAP_ALL_ACCESS, FALSE, kTraceMappingName);
        if (mapping)
        {
            auto* view = static_cast<TraceMapping*>(MapViewOfFile(
                mapping, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(TraceMapping)));
            if (view && view->magic == kTraceMagic && view->version == 1 &&
                view->capacity == kTraceCapacity)
            {
                gTraceMappingHandle = mapping;
                gTraceMapping = view;
            }
            else
            {
                if (view)
                    UnmapViewOfFile(view);
                CloseHandle(mapping);
            }
        }
    }
    if (!gTraceMapping || InterlockedCompareExchange(&gTraceMapping->enabled, 0, 0) == 0)
        return 0;
    return InterlockedIncrement64(&gTraceMapping->nextSequence);
}

void PublishTrace(LONGLONG sequence, const BYTE* data, DWORD length,
                  TraceOutcome outcome, DWORD error, DWORD headerBytes, DWORD payloadBytes,
                  LONGLONG counter, DWORD threadId = 0)
{
    if (!sequence || !gTraceMapping)
        return;
    auto& entry = gTraceMapping->entries[(sequence - 1) % kTraceCapacity];
    InterlockedExchange64(&entry.committedSequence, -sequence);
    entry.counter = counter;
    entry.processId = GetCurrentProcessId();
    entry.threadId = threadId ? threadId : GetCurrentThreadId();
    entry.length = length;
    entry.outcome = outcome;
    entry.error = error;
    entry.headerBytes = headerBytes;
    entry.payloadBytes = payloadBytes;
    std::memset(entry.prefix, 0, sizeof(entry.prefix));
    std::memcpy(entry.prefix, data, std::min<DWORD>(length, sizeof(entry.prefix)));
    InterlockedExchange64(&entry.committedSequence, sequence);
}

void ClosePipeLocked()
{
    if (gPipe != INVALID_HANDLE_VALUE)
    {
        CloseHandle(gPipe);
        gPipe = INVALID_HANDLE_VALUE;
        InterlockedExchange(&gPipeConnected, 0);
    }
}

bool EnsurePipeLocked()
{
    if (gPipe != INVALID_HANDLE_VALUE)
        return true;

    // This is called only by the outbound worker. Admission never performs
    // pipe writes; waiting for pipe capacity cannot stall a DAW MIDI caller.
    if (!WaitNamedPipeW(kPipeName, 1))
        return false;

    HANDLE pipe = CreateFileW(
        kPipeName,
        GENERIC_WRITE,
        0,
        nullptr,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        nullptr);
    if (pipe == INVALID_HANDLE_VALUE)
        return false;

    DWORD mode = PIPE_READMODE_BYTE | PIPE_WAIT;
    if (!SetNamedPipeHandleState(pipe, &mode, nullptr, nullptr))
    {
        CloseHandle(pipe);
        return false;
    }

    gPipe = pipe;
    InterlockedExchange(&gPipeConnected, 1);
    return true;
}

#ifdef ABMSYNTH_MAPPER
bool DefaultRoutingEnabled()
{
    // WinMM default routing remains a user choice in every Windows API mode.
    // Services can deliver MIDI_MAPPER through this driver. Issue1062 warns
    // that SDK/UMP-correlated port numbers can differ;it is not an inability
    // to route WinMM messages. Do not silently override the stored request.
    DWORD enabled = 1;
    DWORD bytes = sizeof(enabled);
    // Missing preference means On. A changed preference is visible on fresh
    // WinMM enumeration; existing MIDI hosts must close/reopen their devices.
    RegGetValueW(HKEY_CURRENT_USER,L"Software\\ABMSynth\\MIDIMapper",L"Enabled",
        RRF_RT_REG_DWORD|RRF_SUBKEY_WOW6464KEY,nullptr,&enabled,&bytes);
    return enabled != 0;
}
#endif
bool IsABMSynthRunning()
{
    // Returning zero devices while no server instance exists makes this
    // registered WinMM driver disappear from fresh device enumeration. This
    // keeps the endpoint app-scoped without modifying protected registry keys
    // or requiring elevation whenever ABMSynth starts or exits.
    // 0 means server DEFAULT timeout, not polling. Bound host enumeration and
    // pre-connection admission even when all receiver instances are busy.
    return WaitNamedPipeW(kPipeName, 1) != FALSE;
}

DWORD ForwardToABMSynth(const BYTE* data, DWORD length, LONGLONG ingressCounter, DWORD ingressThread, bool* reconnectable = nullptr,unsigned port=0)
{
    if (!data || length == 0 || length > kMaxPayloadBytes)
        return MMSYSERR_INVALPARAM;

    LARGE_INTEGER clock{};
    clock.QuadPart = ingressCounter;
    AcquireSRWLockExclusive(&gPipeLock);
    const LONGLONG sequence = ReserveTraceSequenceLocked();
    TraceOutcome outcome = kNoPipe;
    DWORD error = 0;
    DWORD headerBytes = 0;
    DWORD payloadBytes = 0;
    for (unsigned attempt = 0; attempt < 2; ++attempt)
    {
        if (!EnsurePipeLocked()) { error = GetLastError(); break; }
        // Preserve admission time through queue/pipe backpressure and reconnect.
        // Identity belongs to the ordered writer; retries retain the same ID.
        const ULONGLONG eventSequence = attempt ? gTransportSequence : ++gTransportSequence;
        TimedFrameHeader timedHeader{ kTimedFrameMagic, length,
            static_cast<ULONGLONG>(ingressCounter), gClockFrequency, gProducerEpoch, eventSequence,
            sequence, static_cast<ULONGLONG>(InterlockedCompareExchange64(&gTransportLosses,0,0)),
            GetCurrentProcessId(), port?0x80000000u|port:0 };
        const void* header = &timedHeader;
        const DWORD headerLength = sizeof(timedHeader);
        const BOOL headerOk = WriteFile(gPipe, header, headerLength, &headerBytes, nullptr);
        if (!headerOk || headerBytes != headerLength)
        {
            error = headerOk ? ERROR_WRITE_FAULT : GetLastError();
            outcome = kHeaderFailure;
        }
        else
        {
            const BOOL payloadOk = WriteFile(gPipe, data, length, &payloadBytes, nullptr);
            if (!payloadOk || payloadBytes != length)
            {
                error = payloadOk ? ERROR_WRITE_FAULT : GetLastError();
                outcome = kPayloadFailure;
            }
            else
            {
                outcome = kDelivered;
                error = 0;
            }
        }
        if (outcome != kDelivered)
            ClosePipeLocked();
        // ABMSynth may restart while a WinMM host retains its device handle.
        // Retry only a disconnected pipe with NO complete header written:
        // payload was never attempted, hence no complete MIDI frame can have
        // reached the prior receiver. Preserve original ingress time/order.
        // Partial headers/payload failures remain explicit, never replayed.
        if (outcome == kDelivered || headerBytes != 0 ||
            (error != ERROR_BROKEN_PIPE && error != ERROR_PIPE_NOT_CONNECTED && error != ERROR_NO_DATA))
            break;
        headerBytes = payloadBytes = 0;
        outcome = kNoPipe;
    }
    PublishTrace(sequence, data, length, outcome, error, headerBytes, payloadBytes, clock.QuadPart, ingressThread);
    if (outcome != kDelivered) RecordTransportLoss(data,length,static_cast<DWORD>(outcome),error,ingressCounter);
    if (reconnectable) *reconnectable = outcome == kNoPipe ||
        (headerBytes == 0 && (error == ERROR_BROKEN_PIPE || error == ERROR_PIPE_NOT_CONNECTED || error == ERROR_NO_DATA));
    ReleaseSRWLockExclusive(&gPipeLock);
    return outcome == kDelivered ? MMSYSERR_NOERROR : MIDIERR_NOTREADY;
}

DWORD ShortMessageLength(DWORD packedMessage)
{
    const BYTE status = static_cast<BYTE>(packedMessage & 0xff);
    if (status < 0x80)
        return 0;
    if (status >= 0xf8 || status == 0xf6)
        return 1;
    if ((status & 0xf0) == 0xc0 || (status & 0xf0) == 0xd0 || status == 0xf1 || status == 0xf3)
        return 2;
    if (status == 0xf2)
        return 3;
    // SysEx start/end are long messages; forward a lone system byte only if
    // a non-conforming host sends one through MODM_DATA.
    return 3;
}

void Notify(const Instance* instance, UINT message, DWORD_PTR param1 = 0)
{
    if (!instance || !instance->openDesc.dwCallback)
        return;
    DriverCallback(
        instance->openDesc.dwCallback,
        instance->callbackFlags,
        reinterpret_cast<HDRVR>(instance->openDesc.hMidi),
        message,
        instance->openDesc.dwInstance,
        param1,
        0);
}

// Product admission bounds, not MIDI/BASS protocol maxima. One emergency
//96-byte Reset entry is reserved even at ordinary queue saturation. Short
//messages allocate nothing; large payload copies belong to the caller, not
//the synth's real-time renderer. Complete frames share one ordered writer.
constexpr size_t kOutboundCapacity = 2048;
constexpr DWORD kOutboundBytes = 4 * 1024 * 1024;
struct Outbound
{
    Instance* owner{};
    DWORD length{};
    BYTE inlineBytes[3]{};
    BYTE* large{};
    LONGLONG counter{};
    DWORD thread{};
    const BYTE* data() const { return large ? large : inlineBytes; }
};
SRWLOCK gOutboundLock = SRWLOCK_INIT;
SRWLOCK gSenderLifecycle = SRWLOCK_INIT;
SRWLOCK gResetLock = SRWLOCK_INIT;
std::array<Outbound, kOutboundCapacity> gOutbound{};
size_t gOutboundHead = 0, gOutboundCount = 0;
DWORD gOutboundSize = 0, gOpenInstances = 0;
HANDLE gWorkEvent = nullptr, gStopEvent = nullptr, gSenderThread = nullptr;
bool gPaused = false;
Instance* gInFlight = nullptr;

void CompleteOutbound(Outbound& item)
{
    delete[] item.large;
    item.large = nullptr;
    InterlockedDecrement(&item.owner->pending);
}

DWORD WINAPI SenderThread(void*)
{
    const HANDLE waits[] = { gStopEvent, gWorkEvent };
    for (;;)
    {
        if (WaitForMultipleObjects(2, waits, FALSE, INFINITE) == WAIT_OBJECT_0) return 0;
        for (;;)
        {
            AcquireSRWLockExclusive(&gOutboundLock);
            if (gPaused || !gOutboundCount)
            {
                ReleaseSRWLockExclusive(&gOutboundLock);
                break;
            }
            Outbound item = gOutbound[gOutboundHead];
            gOutbound[gOutboundHead] = {};
            gOutboundHead = (gOutboundHead + 1) % kOutboundCapacity;
            --gOutboundCount; gOutboundSize -= item.length;
            gInFlight = item.owner;
            ReleaseSRWLockExclusive(&gOutboundLock);
            if (!InterlockedCompareExchange(&item.owner->failed, 0, 0))
            {
                bool reconnectable = false;
                const DWORD result = ForwardToABMSynth(item.data(), item.length, item.counter, item.thread, &reconnectable,item.owner->port);
                if (result)
                {
                    InterlockedExchange(&item.owner->reconnectable, reconnectable ? 1 : 0);
                    InterlockedExchange(&item.owner->failed, static_cast<LONG>(result));
                }
            }
            else RecordTransportLoss(item.data(),item.length,7,MIDIERR_NOTREADY,item.counter);
            CompleteOutbound(item);
            AcquireSRWLockExclusive(&gOutboundLock);
            gInFlight = nullptr;
            ReleaseSRWLockExclusive(&gOutboundLock);
        }
    }
}

bool RetainSender()
{
    AcquireSRWLockExclusive(&gSenderLifecycle);
    if (!gSenderThread)
    {
        LARGE_INTEGER epoch{}, frequency{};
        QueryPerformanceCounter(&epoch); QueryPerformanceFrequency(&frequency);
        gProducerEpoch=epoch.QuadPart; gClockFrequency=frequency.QuadPart; gTransportSequence=0;
        InterlockedExchange64(&gTransportLosses,0);
        CreateOriginHealth();
        gWorkEvent = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        gStopEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        if (gWorkEvent && gStopEvent) gSenderThread = CreateThread(nullptr, 0, SenderThread, nullptr, 0, nullptr);
        if (!gSenderThread)
        {
            if (gWorkEvent) CloseHandle(gWorkEvent);
            if (gStopEvent) CloseHandle(gStopEvent);
            gWorkEvent = gStopEvent = nullptr;
            CloseOriginHealth();
            ReleaseSRWLockExclusive(&gSenderLifecycle);
            return false;
        }
    }
    ++gOpenInstances;
    ReleaseSRWLockExclusive(&gSenderLifecycle);
    return true;
}

void ReleaseSender()
{
    AcquireSRWLockExclusive(&gSenderLifecycle);
    if (--gOpenInstances == 0)
    {
        // All instances have drained pending buffers. This is MODM_CLOSE,
        // never DllMain/loader-lock synchronization.
        SetEvent(gStopEvent);
        WaitForSingleObject(gSenderThread, INFINITE);
        CloseHandle(gSenderThread); CloseHandle(gWorkEvent); CloseHandle(gStopEvent);
        gSenderThread = gWorkEvent = gStopEvent = nullptr;
        CloseOriginHealth();
        AcquireSRWLockExclusive(&gPipeLock);
        ClosePipeLocked();
        if (gTraceMapping) UnmapViewOfFile(gTraceMapping);
        if (gTraceMappingHandle) CloseHandle(gTraceMappingHandle);
        gTraceMapping = nullptr; gTraceMappingHandle = nullptr;
        ReleaseSRWLockExclusive(&gPipeLock);
    }
    ReleaseSRWLockExclusive(&gSenderLifecycle);
}

DWORD Enqueue(Instance* owner, const BYTE* data, DWORD length, MIDIHDR* header = nullptr, bool reset = false,
              LONGLONG ingressCounter = 0)
{
    if (!owner || !data || !length || length > kMaxPayloadBytes) return MMSYSERR_INVALPARAM;
    if (!reset && InterlockedCompareExchange(&owner->failed, 0, 0))
    {
        // Receiver can have been absent during restart. Drain/drop the old
        // failed generation BEFORE admitting new data; never replay it later.
        // Only a known failure before any complete frame was sent may recover.
        // No connection/write or unbounded wait runs on the host MIDI caller.
        if (!InterlockedCompareExchange(&owner->reconnectable, 0, 0) ||
            InterlockedCompareExchange(&owner->pending, 0, 0) || !IsABMSynthRunning())
            return MIDIERR_NOTREADY;
        InterlockedExchange(&owner->reconnectable, 0);
        InterlockedExchange(&owner->failed, 0);
    }
    if (!InterlockedCompareExchange(&gPipeConnected,0,0) && !IsABMSynthRunning()) return MIDIERR_NOTREADY;
    // The outbound queue owns a complete COPY before admission succeeds.
    // Return the caller's MIDIHDR synchronously at that ownership boundary.
    // Actual Windows midimap.dll's shadow-header path suppresses a later
    // asynchronous MOM_DONE (qualified by real WinMM tests); no undocumented
    // mapper bits/pointers are modified. MOM_DONE is buffer return, NOT a
    // transport/synthesis acknowledgement. Later write failure stays explicit
    // in the trace. Ambiguous/partial writes require Reset; known no-frame
    // disconnections can recover after old pending data is drained.
    Outbound item{}; item.owner = owner; item.length = length;
    LARGE_INTEGER counter{};
    if (ingressCounter) counter.QuadPart=ingressCounter; else QueryPerformanceCounter(&counter);
    item.counter = counter.QuadPart;
    item.thread = GetCurrentThreadId();
    if (length > sizeof(item.inlineBytes))
    {
        item.large = new (std::nothrow) BYTE[length];
        if (!item.large) { RecordTransportLoss(data,length,5,MMSYSERR_NOMEM,item.counter); return MMSYSERR_NOMEM; }
        std::memcpy(item.large, data, length);
    }
    else std::memcpy(item.inlineBytes, data, length);
    AcquireSRWLockExclusive(&gOutboundLock);
    const DWORD result = !reset && (InterlockedCompareExchange(&owner->resetting, 0, 0)
        || InterlockedCompareExchange(&owner->failed, 0, 0)) ? MIDIERR_NOTREADY :
        gOutboundCount >= kOutboundCapacity - (reset ? 0 : 1) ||
        gOutboundSize + length > kOutboundBytes - (reset ? 0 : 96) ? MIDIERR_NOTREADY : MMSYSERR_NOERROR;
    if (result)
    {
        RecordTransportLoss(data,length,6,result,item.counter);
        ReleaseSRWLockExclusive(&gOutboundLock); delete[] item.large; return result;
    }
    if (header)
    {
        InterlockedAnd(reinterpret_cast<volatile LONG*>(&header->dwFlags), ~static_cast<LONG>(MHDR_DONE));
        InterlockedOr(reinterpret_cast<volatile LONG*>(&header->dwFlags), MHDR_INQUEUE);
    }
    InterlockedIncrement(&owner->pending);
    gOutbound[(gOutboundHead + gOutboundCount) % kOutboundCapacity] = item;
    ++gOutboundCount; gOutboundSize += length;
    ReleaseSRWLockExclusive(&gOutboundLock);
    if (header)
    {
        InterlockedAnd(reinterpret_cast<volatile LONG*>(&header->dwFlags), ~static_cast<LONG>(MHDR_INQUEUE));
        InterlockedOr(reinterpret_cast<volatile LONG*>(&header->dwFlags), MHDR_DONE);
        Notify(owner,MOM_DONE,reinterpret_cast<DWORD_PTR>(header));
    }
    SetEvent(gWorkEvent);
    return MMSYSERR_NOERROR;
}

DWORD ResetOutbound(Instance* owner)
{
    if (!owner) return MMSYSERR_INVALHANDLE;
    AcquireSRWLockExclusive(&gResetLock);
    InterlockedExchange(&owner->resetting, 1);
    std::vector<Outbound> retired;
    try { retired.reserve(kOutboundCapacity); }
    catch (...) { InterlockedExchange(&owner->resetting, 0); ReleaseSRWLockExclusive(&gResetLock); return MMSYSERR_NOMEM; }
    AcquireSRWLockExclusive(&gOutboundLock);
    gPaused = true;
    const size_t originalCount = gOutboundCount;
    size_t kept = 0; DWORD bytes = 0;
    for (size_t index = 0; index < originalCount; ++index)
    {
        Outbound item = gOutbound[(gOutboundHead + index) % kOutboundCapacity];
        if (item.owner == owner) retired.push_back(item);
        else { gOutbound[(gOutboundHead + kept++) % kOutboundCapacity] = item; bytes += item.length; }
    }
    gOutboundCount = kept; gOutboundSize = bytes;
    const bool ownWrite = gInFlight == owner;
    ReleaseSRWLockExclusive(&gOutboundLock);
    for (auto& item : retired) CompleteOutbound(item);
    if (ownWrite) CancelSynchronousIo(gSenderThread);
    const ULONGLONG until = GetTickCount64() + 2000;
    while (InterlockedCompareExchange(&owner->pending, 0, 0) && GetTickCount64() < until)
    {
        // The first cancellation can race just before WriteFile enters the
        // kernel. Retry only while this instance is still in-flight; never
        // cancel a different producer's packet.
        AcquireSRWLockShared(&gOutboundLock);
        const bool cancelOwn = gInFlight == owner;
        ReleaseSRWLockShared(&gOutboundLock);
        if (cancelOwn) CancelSynchronousIo(gSenderThread);
        Sleep(1);
    }
    DWORD result = MIDIERR_NOTREADY;
    if (!InterlockedCompareExchange(&owner->pending, 0, 0))
    {
        InterlockedExchange(&owner->failed, 0);
        InterlockedExchange(&owner->reconnectable, 0);
        BYTE reset[96]{};
        for (BYTE channel = 0; channel < 16; ++channel)
        {
            const BYTE events[] = {static_cast<BYTE>(0xb0 | channel), 64, 0, static_cast<BYTE>(0xb0 | channel), 120, 0};
            std::memcpy(reset + channel * 6, events, sizeof(events));
        }
        result = Enqueue(owner, reset, sizeof(reset), nullptr, true);
    }
    AcquireSRWLockExclusive(&gOutboundLock); gPaused = false; ReleaseSRWLockExclusive(&gOutboundLock);
    InterlockedExchange(&owner->resetting, 0); SetEvent(gWorkEvent);
    ReleaseSRWLockExclusive(&gResetLock);
    return result;
}

DWORD PortSetting(const wchar_t* name,DWORD fallback){
#ifdef ABMSYNTH_PORTS_PROBE
    (void)fallback;
    return wcscmp(name,L"Count")==0?8:255;
#else
    DWORD value=fallback,bytes=sizeof(value);
    RegGetValueW(HKEY_CURRENT_USER,L"Software\\ABMSynth\\MIDIPorts",name,RRF_RT_REG_DWORD|RRF_SUBKEY_WOW6464KEY,nullptr,&value,&bytes);return value;
#endif
}
unsigned PortCount(bool input){
    const auto count=std::min(8u,std::max<unsigned>(1u,PortSetting(L"Count",1)));
    const auto mask=PortSetting(input?L"OutMask":L"InMask",255);unsigned result=0;
    for(unsigned i=0;i<count;++i)if(mask&(1u<<i))++result;return result;
}
unsigned PortAddress(bool input,unsigned index){
    const auto count=std::min(8u,std::max<unsigned>(1u,PortSetting(L"Count",1)));
    const auto mask=PortSetting(input?L"OutMask":L"InMask",255);
    for(unsigned i=0;i<count;++i)if(mask&(1u<<i)){if(!index)return i;--index;}return 8;
}
void PortName(unsigned port,bool input,wchar_t* name){
    wchar_t key[32]{},base[28]=L"ABMSynth MIDI";swprintf_s(key,L"Name%u",port);DWORD bytes=sizeof(base);
    if(port)_snwprintf_s(base,_countof(base),_TRUNCATE,L"ABMSynth MIDI %u",port+1);
    RegGetValueW(HKEY_CURRENT_USER,L"Software\\ABMSynth\\MIDIPorts",key,RRF_RT_REG_SZ|RRF_SUBKEY_WOW6464KEY,nullptr,base,&bytes);
    base[27]=0;_snwprintf_s(name,MAXPNAMELEN,_TRUNCATE,L"%s %s",base,input?L"Out":L"In");
}
void PortNameAnsi(const wchar_t* name,char* output){
    int length=lstrlenW(name);output[0]=0;
    while(length>0){const int bytes=WideCharToMultiByte(CP_ACP,0,name,length,output,MAXPNAMELEN-1,nullptr,nullptr);
        if(bytes){output[bytes]=0;return;}--length;}
}
DWORD FillCapabilities(DWORD_PTR param, DWORD_PTR size,unsigned port)
{
    if (!param || size < sizeof(MIDIOUTCAPSA))
        return MMSYSERR_INVALPARAM;

    if (size >= sizeof(MIDIOUTCAPSW))
    {
        auto* caps = reinterpret_cast<MIDIOUTCAPSW*>(param);
        std::memset(caps, 0, sizeof(*caps));
        caps->wMid = 1; // Microsoft manufacturer identifier; no custom MMID exists.
        caps->wPid = 0x4142; // "AB"
        caps->vDriverVersion = 0x0100;
        PortName(port,false,caps->szPname);
        caps->wTechnology =
#if defined(ABMSYNTH_MAPPER)
            DefaultRoutingEnabled()&&port==PortAddress(false,0) ? MOD_MAPPER : MOD_MIDIPORT;
#elif defined(ABMSYNTH_TRACE_SMOKE)
            MOD_MAPPER;
#else
            MOD_MIDIPORT;
#endif
        caps->wChannelMask = 0xffff;
        caps->dwSupport = 0; // No WinMM master-volume implementation advertised.
    }
    else
    {
        auto* caps = reinterpret_cast<MIDIOUTCAPSA*>(param);
        std::memset(caps, 0, sizeof(*caps));
        caps->wMid = 1;
        caps->wPid = 0x4142;
        caps->vDriverVersion = 0x0100;
        wchar_t name[MAXPNAMELEN]{};PortName(port,false,name);
        PortNameAnsi(name,caps->szPname);
        caps->wTechnology =
#if defined(ABMSYNTH_MAPPER)
            DefaultRoutingEnabled()&&port==PortAddress(false,0) ? MOD_MAPPER : MOD_MIDIPORT;
#elif defined(ABMSYNTH_TRACE_SMOKE)
            MOD_MAPPER;
#else
            MOD_MIDIPORT;
#endif
        caps->wChannelMask = 0xffff;
        caps->dwSupport = 0;
    }
    return MMSYSERR_NOERROR;
}
} // namespace

#include "midi_input_endpoint.h"

extern "C" __declspec(dllexport) LRESULT CALLBACK DriverProc(
    DWORD_PTR,
    HDRVR,
    UINT message,
    LPARAM,
    LPARAM)
{
    switch (message)
    {
    case DRV_LOAD:
    case DRV_ENABLE:
    case DRV_OPEN:
    case DRV_CLOSE:
    case DRV_DISABLE:
    case DRV_FREE:
        return 1;
    default:
        return 0;
    }
}

extern "C" __declspec(dllexport) DWORD WINAPI modMessage(
    UINT deviceId,
    UINT message,
    DWORD_PTR user,
    DWORD_PTR param1,
    DWORD_PTR param2)
{
    // First instruction-level observation in the owned driver entry point,
    // before validation, allocation, queue locks, diagnostics or pipe writes.
    // No claim about time spent in the sender/WinMM before entering this DLL.
    LARGE_INTEGER ingress{};
    if (message==MODM_DATA || message==MODM_LONGDATA) QueryPerformanceCounter(&ingress);
#ifdef ABMSYNTH_TRACE_SMOKE
    // Bounded shared-memory ABI trace for the temporary mapper experiment.
    // No disk I/O; production builds contain none of this instrumentation.
    HANDLE mapping = OpenFileMappingW(FILE_MAP_ALL_ACCESS,FALSE,L"Local\\ABMSynthMapperCallProbe");
    if (mapping) {
        auto* memory = static_cast<DWORD*>(MapViewOfFile(mapping,FILE_MAP_ALL_ACCESS,0,0,65536));
        if (memory) {
            const LONG index = InterlockedIncrement(reinterpret_cast<volatile LONG*>(memory));
            if (index>0 && index<8191) { memory[index*2]=deviceId; memory[index*2+1]=message; }
            UnmapViewOfFile(memory);
        }
        CloseHandle(mapping);
    }
#endif
    if (deviceId >= PortCount(false) && (message==MODM_OPEN||message==MODM_GETDEVCAPS)
#ifdef ABMSYNTH_TRACE_SMOKE
        // Isolated mapper ABI experiment: WinMM passes its mapper sentinel,
        // not ordinary endpoint device 0. Production endpoint is unchanged.
        && deviceId != static_cast<UINT>(MIDI_MAPPER) && deviceId != 0xffff
#endif
       )
        return MMSYSERR_BADDEVICEID;

    auto* instance = reinterpret_cast<Instance*>(user);
    switch (message)
    {
    case MODM_GETNUMDEVS:
        return IsABMSynthRunning() ? PortCount(false) : 0;

    case MODM_GETDEVCAPS:
        return FillCapabilities(param1, param2,PortAddress(false,deviceId));

    case MODM_OPEN:
    {
        if (!user || !param1)
            return MMSYSERR_INVALPARAM;
        if (!IsABMSynthRunning())
            return MMSYSERR_NOTENABLED;
        auto* created = new (std::nothrow) Instance();
        if (!created)
            return MMSYSERR_NOMEM;
        created->port=PortAddress(false,deviceId);
        if (!RetainSender()) { delete created; return MMSYSERR_NOMEM; }
        created->openDesc = *reinterpret_cast<MIDIOPENDESC*>(param1);
        // MODM_OPEN carries WinMM CALLBACK_* in the high word. DriverCallback
        // consumes DCB_* in the low word (mmddk.h); don't silently omit MOM_DONE.
        created->callbackFlags = HIWORD(static_cast<DWORD>(param2)) & 7;
        *reinterpret_cast<DWORD_PTR*>(user) = reinterpret_cast<DWORD_PTR>(created);
        Notify(created, MOM_OPEN);
        return MMSYSERR_NOERROR;
    }

    case MODM_CLOSE:
        if (!instance)
            return MMSYSERR_INVALHANDLE;
        {
            const ULONGLONG until = GetTickCount64() + 2000;
            while (InterlockedCompareExchange(&instance->pending, 0, 0) && GetTickCount64() < until) Sleep(1);
        }
        if (InterlockedCompareExchange(&instance->pending, 0, 0)) return MIDIERR_STILLPLAYING;
        Notify(instance, MOM_CLOSE);
        delete instance;
        ReleaseSender();
        return MMSYSERR_NOERROR;

    case MODM_DATA:
    {
        if (!instance)
            return MMSYSERR_INVALHANDLE;
        const DWORD packed = static_cast<DWORD>(param1);
        const DWORD length = ShortMessageLength(packed);
        BYTE bytes[3] = {
            static_cast<BYTE>(packed & 0xff),
            static_cast<BYTE>((packed >> 8) & 0xff),
            static_cast<BYTE>((packed >> 16) & 0xff)
        };
        if (length)
            return Enqueue(instance, bytes, length, nullptr, false, ingress.QuadPart);
        return MMSYSERR_INVALPARAM;
    }

    case MODM_LONGDATA:
    {
        if (!instance || !param1)
            return MMSYSERR_INVALPARAM;
        auto* header = reinterpret_cast<MIDIHDR*>(param1);
        if (!header->lpData || header->dwBufferLength == 0 || header->dwBufferLength > kMaxPayloadBytes)
            return MMSYSERR_INVALPARAM;
        if (!(header->dwFlags & MHDR_PREPARED)) return MIDIERR_UNPREPARED;
        if (header->dwFlags & MHDR_INQUEUE) return MIDIERR_NOTREADY;
        return Enqueue(instance, reinterpret_cast<const BYTE*>(header->lpData), header->dwBufferLength, header, false, ingress.QuadPart);
    }

    case MODM_PREPARE:
        if (!instance || !param1)
            return MMSYSERR_INVALPARAM;
        reinterpret_cast<MIDIHDR*>(param1)->dwFlags |= MHDR_PREPARED;
        return MMSYSERR_NOERROR;

    case MODM_UNPREPARE:
        if (!instance || !param1)
            return MMSYSERR_INVALPARAM;
        if (reinterpret_cast<MIDIHDR*>(param1)->dwFlags & MHDR_INQUEUE) return MIDIERR_STILLPLAYING;
        reinterpret_cast<MIDIHDR*>(param1)->dwFlags &= ~(MHDR_PREPARED | MHDR_DONE);
        return MMSYSERR_NOERROR;

    case MODM_RESET:
        return ResetOutbound(instance);
    case MODM_PAUSE:
    case MODM_RESTART:
    case MODM_STOP:
    case MODM_CACHEPATCHES:
    case MODM_CACHEDRUMPATCHES:
        return MMSYSERR_NOERROR;

    case MODM_GETVOLUME:
        if (!param1)
            return MMSYSERR_INVALPARAM;
        *reinterpret_cast<DWORD*>(param1) = 0xffffffff;
        return MMSYSERR_NOERROR;

    case MODM_SETVOLUME:
        return MMSYSERR_NOERROR;

    default:
        return MMSYSERR_NOTSUPPORTED;
    }
}
