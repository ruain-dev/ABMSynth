// MIDI performance variation. Control/input actor only,never a DSP callback.
// Installed bassmidi.h:STRUCT|TIME positions are relative bytes;CANCEL removes
// pending events. Preserve RAW decoder state with a synchronously rejected
// RAW note before sending the timed STRUCT event. The filter fast path only
// reads this thread-local flag;it allocates nothing and never waits.
#pragma once
#include <map>
#include <deque>
#include <limits>
static thread_local bool humanizationSeed=false;
static thread_local bool humanizationFile=false;
struct HumanFileScope {bool previous;explicit HumanFileScope(bool file):previous(humanizationFile){humanizationFile=file;}~HumanFileScope(){humanizationFile=previous;}};
struct HumanPair {unsigned ms=0;unsigned channel=0;unsigned long long base=0,id=0,due=0,onDue=0,onOrigin=0,delayBytes=0;bool grouped=false;};
struct HumanSpan {unsigned offset,channel,param;std::shared_ptr<HumanPair> pair;};
class MidiHumanization {
    struct Pending {BASS_MIDI_EVENT event;unsigned long long position,base;unsigned mode;std::shared_ptr<HumanPair> pair;};
    std::map<unsigned,std::deque<std::shared_ptr<HumanPair>>> held;
    std::vector<Pending> pending;
    std::array<std::weak_ptr<HumanPair>,16384> lastPair{};
    unsigned boundStream=0;
    unsigned long long randomState=0x7147932491e93a5bull,nextPair=0;
    unsigned captured=0,frameConfiguration=0,lastPort=UINT_MAX;
    unsigned long long lastAttackDue=0,lastPid=0,lastEpoch=0;bool attackFloor=false;
    struct Group {unsigned configuration=0;unsigned long long stamp=0,pid=0,epoch=0,cursor=0,due=0;std::array<bool,2048> keys{};};
    std::array<Group,8> groups{};
    unsigned long long frameStamp=0,framePid=0,frameEpoch=0,frequency=0;
    void clearGroups(unsigned channel=UINT_MAX){if(channel==UINT_MAX)for(auto& group:groups)group.stamp=0;else groups[channel/16].stamp=0;}
    QWORD(WINAPI* position)(DWORD,DWORD)=nullptr;
    QWORD(WINAPI* secondsToBytes)(DWORD,double)=nullptr;
    BOOL(WINAPI* streamLock)(DWORD,BOOL)=nullptr;
    DWORD(WINAPI* events)(HSTREAM,DWORD,void*,DWORD)=nullptr;
    unsigned bounded(unsigned bound){for(;;){randomState+=0x9e3779b97f4a7c15ull;auto z=randomState;
        z=(z^(z>>30))*0xbf58476d1ce4e5b9ull;z=(z^(z>>27))*0x94d049bb133111ebull;z^=z>>31;
        if(z>=(0ull-static_cast<unsigned long long>(bound))%bound)return static_cast<unsigned>(z%bound);}}
public:
    std::array<std::atomic<unsigned long long>,2> clearMask{};
    void cleared(unsigned channel){if(humanizationFile)return;if(channel==UINT_MAX){clearMask[0].store(~0ull);clearMask[1].store(~0ull);}else clearMask[channel/64].fetch_or(1ull<<(channel%64));}
    bool failed=false;
    std::atomic<unsigned> configuration{0};
    std::vector<HumanSpan> spans;
    void bind(void* raw,void* getPosition,void* convert,void* lock){
        events=reinterpret_cast<DWORD(WINAPI*)(HSTREAM,DWORD,void*,DWORD)>(raw);
        position=reinterpret_cast<QWORD(WINAPI*)(DWORD,DWORD)>(getPosition);
        secondsToBytes=reinterpret_cast<QWORD(WINAPI*)(DWORD,double)>(convert);
        streamLock=reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(lock);
    }
    bool configure(unsigned ms,unsigned velocity,bool preserve=false){
        if(ms>300||velocity>64||((ms||velocity)&&(!events||!position||!secondsToBytes||!streamLock)))return false;
        configuration.store(ms|(velocity<<16)|(preserve?1u<<24:0),std::memory_order_release);return true;
    }
    unsigned timing()const{return configuration.load(std::memory_order_acquire)&65535u;}
    bool preserves()const{return (configuration.load(std::memory_order_acquire)&(1u<<24))!=0;}
    void boundary(unsigned channel){clearGroups(channel);}
    unsigned velocity(unsigned value){const auto amount=(frameConfiguration>>16)&255u;
        return !value||!amount?value:static_cast<unsigned>(std::clamp(static_cast<int>(value)+static_cast<int>(bounded(amount*2+1))-static_cast<int>(amount),1,127));}
    std::shared_ptr<HumanPair> note(unsigned channel,unsigned key,bool on)try{
        if(humanizationFile)return {};
        const unsigned address=channel*128+key,amount=frameConfiguration&65535u;
        auto found=held.find(address);
        if(on){
            auto previous=lastPair[address].lock();
            if(!amount&&found==held.end()&&!previous&&!attackFloor)return {};
            const auto cursor=position(boundStream,BASS_POS_BYTE|BASS_POS_DECODE),bytesPerSecond=secondsToBytes(boundStream,1.);
            if(attackFloor&&lastAttackDue<cursor)attackFloor=false;
            if(!amount&&found==held.end()&&!previous&&!attackFloor)return {};
            if(lastPort!=channel/16||lastPid!=framePid||lastEpoch!=frameEpoch)clearGroups();
            lastPort=channel/16;lastPid=framePid;lastEpoch=frameEpoch;
            auto pair=std::make_shared<HumanPair>();pair->ms=amount?bounded(amount*2+1):0;pair->channel=channel;pair->id=++nextPair;
            // Same-key retriggers cannot overtake an already reserved attack or
            // release. A live amount edit never moves an older reservation.
            pair->due=cursor+secondsToBytes(boundStream,pair->ms/1000.);
            if(previous)pair->due=std::max(pair->due,previous->due);
            // Compare exact sample positions. Rounding the floor up to whole
            // milliseconds on every packet would accumulate artificial delay.
            if(attackFloor)pair->due=std::max(pair->due,lastAttackDue);
            if(amount&&(frameConfiguration&(1u<<24))){
                auto& group=groups[channel/16];const unsigned local=address%2048;
                const bool joins=group.stamp&&frameStamp>=group.stamp&&frameStamp-group.stamp<=frequency/200&&
                    group.pid==framePid&&group.epoch==frameEpoch&&group.configuration==frameConfiguration&&!group.keys[local];
                if(joins){const auto due=group.due+secondsToBytes(boundStream,static_cast<double>(frameStamp-group.stamp)/frequency);
                    if((!previous||previous->due<=due)&&lastAttackDue<=due)pair->due=due;else group.stamp=0;}
                if(!joins||!group.stamp){group.keys.fill(false);group.stamp=frameStamp;group.pid=framePid;group.epoch=frameEpoch;
                    group.configuration=frameConfiguration;group.cursor=cursor;group.due=pair->due;}
                group.keys[local]=true;pair->grouped=true;pair->onOrigin=frameStamp;pair->onDue=pair->due;
            }
            pair->delayBytes=pair->due>cursor?pair->due-cursor:0;
            pair->ms=static_cast<unsigned>((pair->delayBytes*1000+bytesPerSecond-1)/bytesPerSecond);
            pair->onDue=pair->due;lastAttackDue=pair->due;attackFloor=true;
            lastPair[address]=pair;
            held[address].push_back(pair);++captured;return pair;
        }
        if(found==held.end()||found->second.empty())return {};
        auto pair=found->second.front();const auto cursor=position(boundStream,BASS_POS_BYTE|BASS_POS_DECODE);
        pair->due=cursor+pair->delayBytes;
        if(pair->grouped&&frameStamp>=pair->onOrigin){pair->due=pair->onDue+secondsToBytes(boundStream,static_cast<double>(frameStamp-pair->onOrigin)/frequency);
            pair->delayBytes=pair->due>cursor?pair->due-cursor:0;}
        found->second.pop_front();if(found->second.empty())held.erase(found);--captured;return pair;
    }
    catch(const std::bad_alloc&){failed=true;return {};}
    void begin(unsigned stream,const unsigned long long* origin=nullptr){
        if(!frequency){LARGE_INTEGER f{};QueryPerformanceFrequency(&f);frequency=static_cast<unsigned long long>(f.QuadPart);}
        LARGE_INTEGER now{};QueryPerformanceCounter(&now);
        frameStamp=origin&&(origin[0]||origin[1])?(origin[0]?origin[0]:origin[1]):static_cast<unsigned long long>(now.QuadPart);
        framePid=origin?origin[2]:0;frameEpoch=origin?origin[3]:0;
        boundStream=stream;failed=false;spans.clear();frameConfiguration=configuration.load(std::memory_order_acquire);
        if(boundStream&&!pending.empty()){const auto cursor=position(boundStream,BASS_POS_BYTE|BASS_POS_DECODE);
            if(cursor!=QWORD(-1))pending.erase(std::remove_if(pending.begin(),pending.end(),[&](const Pending& p){return p.position<cursor;}),pending.end());}}
    unsigned delayAt(unsigned offset)const{const auto found=std::lower_bound(spans.begin(),spans.end(),offset,[](const HumanSpan& item,unsigned at){return item.offset<at;});return found!=spans.end()&&found->offset==offset?found->pair->ms:0;}
    void span(unsigned offset,unsigned channel,unsigned key,unsigned value,const std::shared_ptr<HumanPair>& pair){
        // A zero-delay captured note must use the same timed queue. An earlier
        // note at exactly cursor has not necessarily reached PCM yet.
        if(pair)try{spans.push_back({offset,channel,key|(value<<8),pair});}catch(const std::bad_alloc&){failed=true;}
    }
    // Called only for ranges of the original output buffer. Unchanged bytes
    // still reach the original RAW API;only identified delayed notes differ.
    DWORD emit(unsigned stream,unsigned mode,unsigned char* data,unsigned length,unsigned base)try{
        boundStream=stream;if(failed)return DWORD(-1);
        if(!events)return DWORD(-1);
        auto first=std::lower_bound(spans.begin(),spans.end(),base,[](const HumanSpan& s,unsigned at){return s.offset<at;});
        if(first==spans.end()||first->offset>=base+length)return events(stream,mode,data,length);
        if(!streamLock(stream,TRUE))return DWORD(-1);
        struct Unlock {unsigned stream;BOOL(WINAPI* call)(DWORD,BOOL);~Unlock(){call(stream,FALSE);}}unlock{stream,streamLock};
        const auto cursor=position(stream,BASS_POS_BYTE|BASS_POS_DECODE);if(cursor==QWORD(-1))return DWORD(-1);
        pending.erase(std::remove_if(pending.begin(),pending.end(),[&](const Pending& p){return p.position<cursor;}),pending.end());
        pending.reserve(pending.size()+static_cast<size_t>(spans.end()-first));
        unsigned at=0,total=0;
        for(auto it=first;it!=spans.end()&&it->offset<base+length;++it){
            const auto relative=it->offset-base;
            if(relative>at){const DWORD n=events(stream,mode,data+at,relative-at);if(n==DWORD(-1))return n;total+=n;}
            if(relative+3>length)return DWORD(-1);
            const auto due=(it->param>>8)?it->pair->onDue:it->pair->due;
            const auto delay=due>cursor?due-cursor:0;if(delay==QWORD(-1)||delay>UINT_MAX)return DWORD(-1);
            // Seed callback MUST run on this actor;FLUSH disables ASYNC for this
            // rejected note only. Future TIME events remain queued per manual.
            // FLUSH can deliver older ASYNC notes. Process those before the
            // seed-only rejection scope,so ordinary notes cannot be discarded.
            if(events(stream,(mode&BASS_MIDI_EVENTS_SYNC)|BASS_MIDI_EVENTS_FLUSH,nullptr,0)==DWORD(-1))return DWORD(-1);
            const bool previous=humanizationSeed;humanizationSeed=true;
            const DWORD seeded=events(stream,mode|BASS_MIDI_EVENTS_FLUSH,data+relative,3);
            humanizationSeed=previous;if(seeded==DWORD(-1))return seeded;
            BASS_MIDI_EVENT event{MIDI_EVENT_NOTE,it->param,it->channel,0,static_cast<DWORD>(delay)};
            unsigned flags=(mode&~(BASS_MIDI_EVENTS_RAW|65535u|BASS_MIDI_EVENTS_ABSTIME))|BASS_MIDI_EVENTS_TIME|BASS_MIDI_EVENTS_FILTER;
            const DWORD sent=events(stream,flags,&event,1);if(sent==DWORD(-1))return sent;
            if(it->param>>8)it->pair->base=cursor;pending.push_back({event,cursor+delay,cursor,flags,it->pair});
            total+=sent;at=relative+3;
        }
        if(at<length){const DWORD n=events(stream,mode,data+at,length-at);if(n==DWORD(-1))return n;total+=n;}
        return total;
    }
    catch(const std::bad_alloc&){failed=true;return DWORD(-1);}
    bool queued()const{return !pending.empty();}
    // The actor's Reset/Panic path owns this mutation. Flush pre-existing async
    // controls,then rebuild unaffected timed notes;no channel loses its queue.
    bool cancel(unsigned stream,unsigned channel=UINT_MAX){
        clearGroups(channel);lastAttackDue=0;attackFloor=false;lastPort=UINT_MAX;
        if(!events||pending.empty()){
            for(auto it=held.begin();it!=held.end();)if(channel==UINT_MAX||it->first/128==channel){captured-=static_cast<unsigned>(it->second.size());it=held.erase(it);}else ++it;
            if(channel==UINT_MAX)for(auto& pair:lastPair)pair.reset();else for(unsigned key=0;key<128;++key)lastPair[channel*128+key].reset();
            cleared(channel);return true;
        }
        if(!streamLock(stream,TRUE))return false;
        struct Unlock {unsigned stream;BOOL(WINAPI* call)(DWORD,BOOL);~Unlock(){call(stream,FALSE);}}unlock{stream,streamLock};
        const unsigned policy=pending.front().mode&BASS_MIDI_EVENTS_SYNC;
        if(events(stream,BASS_MIDI_EVENTS_FLUSH|policy,nullptr,0)==DWORD(-1))return false;
        const auto cursor=position(stream,BASS_POS_BYTE|BASS_POS_DECODE);if(cursor==QWORD(-1))return false;
        std::vector<Pending> keep;try{keep.reserve(pending.size());}catch(const std::bad_alloc&){return false;}
        if(events(stream,BASS_MIDI_EVENTS_CANCEL|policy,nullptr,0)==DWORD(-1))return false;
        auto rows=std::move(pending);pending=std::move(keep);
        for(const auto& row:rows)if(row.position>=cursor&&row.event.chan!=channel&&channel!=UINT_MAX){
            auto event=row.event;event.pos=static_cast<unsigned>(std::min<QWORD>(UINT_MAX,row.position-cursor));
            if(events(stream,row.mode,&event,1)==DWORD(-1))return false;pending.push_back(row);if(event.param>>8){lastAttackDue=std::max(lastAttackDue,row.position);attackFloor=true;}
        }
        for(auto it=held.begin();it!=held.end();)if(channel==UINT_MAX||it->first/128==channel){captured-=static_cast<unsigned>(it->second.size());it=held.erase(it);}else ++it;
        if(channel==UINT_MAX)for(auto& pair:lastPair)pair.reset();else for(unsigned key=0;key<128;++key)lastPair[channel*128+key].reset();
        cleared(channel);return true;
    }
};

// Native file look-ahead. Original RAW bytes/row IDs remain immutable. Only
// the due-order changes;the same ingress actor still applies each original
// frame. Fixed MIDI association is prepared off the actor with file loading.
class FileHumanization {
public:
    struct Ready {double seconds;unsigned index;unsigned long long nominal=0,target=0;};
    struct Later {bool operator()(const Ready& a,const Ready& b)const{return a.target!=b.target?a.target>b.target:a.index>b.index;}};
    std::vector<unsigned> mate,group;
    std::vector<double> groupLower,groupUpper;
    std::vector<double> lower,upper,jitter;
    std::vector<unsigned char> played,cancelled;
    std::vector<Ready> future,history;
    unsigned cursor=0,frontier=0,prefix=0,seenAmount=UINT_MAX;
    double seenSpeed=0;bool active=false;
    unsigned long long randomState=0x5ad48b3a23c1849full;
    unsigned draw(unsigned bound){randomState+=0x9e3779b97f4a7c15ull;auto x=randomState;
        x=(x^(x>>30))*0xbf58476d1ce4e5b9ull;x=(x^(x>>27))*0x94d049bb133111ebull;x^=x>>31;return static_cast<unsigned>(x%bound);}
    static bool resetWire(const unsigned char* p,unsigned size){return (size==6&&p[0]==240&&(p[1]==126||p[1]==127)&&p[3]==9&&(p[4]==1||p[4]==2||p[4]==3))||
        (size>=10&&p[0]==240&&p[1]==65&&p[5]==64&&p[6]==0&&p[7]==127)||
        (size==9&&p[0]==240&&p[1]==67&&p[3]==76&&p[4]==0&&p[5]==0&&p[6]==126);}
    template<class Rows> void prepare(const Rows& rows,const std::vector<unsigned char>& bytes){
        mate.assign(rows.size(),UINT_MAX);lower.assign(rows.size(),0);upper.assign(rows.size(),std::numeric_limits<double>::infinity());
        jitter.assign(rows.size(),std::numeric_limits<double>::quiet_NaN());played.assign(rows.size(),0);cancelled.assign(rows.size(),0);
        std::map<unsigned,std::deque<unsigned>> held;std::array<double,128> previous{};
        for(unsigned i=0;i<rows.size();++i){const auto& r=rows[i];if(!r.size)continue;const auto* p=bytes.data()+r.offset;
            if(p[0]>=240){if(resetWire(p,r.size))previous.fill(r.seconds);continue;}const unsigned type=p[0]&240u,ch=r.port*16+(p[0]&15u);
            if(r.size==3&&(type==0x80u||type==0x90u)){
                const unsigned key=ch*128+p[1];lower[i]=previous[ch];
                if(type==0x90u&&p[2]){mate[i]=i;held[key].push_back(i);}
                else if(!held[key].empty()){mate[i]=held[key].front();held[key].pop_front();}
            }else if(type==0xc0u||(r.size==3&&type==0xb0u&&(p[1]==0||p[1]==32||p[1]==120||p[1]>=123)))previous[ch]=r.seconds;
        }
        std::array<double,128> next;next.fill(std::numeric_limits<double>::infinity());
        for(size_t at=rows.size();at;){const unsigned i=static_cast<unsigned>(--at);const auto& r=rows[i];if(!r.size)continue;const auto* p=bytes.data()+r.offset;
            if(p[0]>=240){if(resetWire(p,r.size))next.fill(r.seconds);continue;}const unsigned type=p[0]&240u,ch=r.port*16+(p[0]&15u);
            if(mate[i]==i)upper[i]=next[ch];
            else if(type==0xc0u||(r.size==3&&type==0xb0u&&(p[1]==0||p[1]==32||p[1]==120||p[1]>=123)))next[ch]=r.seconds;
        }
        double attackCeiling=std::numeric_limits<double>::infinity();
        for(size_t at=rows.size();at;){const unsigned i=static_cast<unsigned>(--at);
            if(mate[i]==i){attackCeiling=std::min(attackCeiling,upper[i]);upper[i]=attackCeiling;}}
        group.assign(rows.size(),UINT_MAX);groupLower.assign(rows.size(),-std::numeric_limits<double>::infinity());groupUpper.assign(rows.size(),std::numeric_limits<double>::infinity());
        std::array<unsigned,8> first;first.fill(UINT_MAX);std::array<std::array<bool,2048>,8> keys{};
        for(unsigned i=0;i<rows.size();++i){const auto& r=rows[i];if(!r.size)continue;const auto* p=bytes.data()+r.offset;
            if(p[0]>=240){if(resetWire(p,r.size))first.fill(UINT_MAX);continue;}
            const unsigned type=p[0]&240u,port=r.port,ch=port*16+(p[0]&15u);
            if(mate[i]==i){for(unsigned other=0;other<8;++other)if(other!=port)first[other]=UINT_MAX;
                const unsigned key=(ch%16)*128+p[1];auto& anchor=first[port];
                if(anchor==UINT_MAX||r.seconds-rows[anchor].seconds>.005+1e-12||keys[port][key]){anchor=i;keys[port].fill(false);}
                keys[port][key]=true;group[i]=anchor;
                groupLower[anchor]=std::max(groupLower[anchor],std::max(0.,lower[i])-r.seconds);
                groupUpper[anchor]=std::min(groupUpper[anchor],upper[i]-r.seconds);
            }else if(type==0xc0u||(r.size==3&&type==0xb0u&&(p[1]==0||p[1]==32||p[1]==120||p[1]>=123)))first[port]=UINT_MAX;
        }
        future.reserve(std::min<size_t>(rows.size(),16384));history.reserve(rows.size());
    }
    void reset(unsigned index){
        cursor=frontier=prefix=index;active=false;future.clear();history.clear();seenAmount=UINT_MAX;seenSpeed=0;
        std::fill(played.begin(),played.end(),static_cast<unsigned char>(0));std::fill_n(played.begin(),std::min<size_t>(index,played.size()),static_cast<unsigned char>(1));
        std::fill(cancelled.begin(),cancelled.end(),static_cast<unsigned char>(0));std::fill(jitter.begin(),jitter.end(),std::numeric_limits<double>::quiet_NaN());
    }
    template<class Rows> void reanchor(const Rows& rows,const std::vector<unsigned char>& bytes,double position,double speed,bool preserve,unsigned long long tick,unsigned long long frequency){
        auto nominal=[&](double stamp){return static_cast<unsigned long long>(std::max(0.,static_cast<double>(tick)+(stamp-position)/speed*frequency));};
        std::sort(future.begin(),future.end(),[](const Ready& a,const Ready& b){return a.index<b.index;});
        std::map<unsigned,unsigned long long> lastOn,lastOff;unsigned long long lastAttack=0;
        for(auto& entry:future){const auto& r=rows[entry.index];if(r.seconds>=position)entry.nominal=nominal(r.seconds);
            entry.target=entry.nominal;
            if(r.size==3&&mate[entry.index]!=UINT_MAX){const auto on=mate[entry.index];const auto* p=bytes.data()+r.offset;
                const unsigned key=(r.port*16+(p[0]&15u))*128+p[1];
                const double shift=preserve&&group[on]!=UINT_MAX&&!std::isnan(jitter[group[on]])?jitter[group[on]]:std::isnan(jitter[on])?0:jitter[on];
                entry.target=static_cast<unsigned long long>(std::max(0.,static_cast<double>(entry.nominal)+shift*frequency/1000));
                if(on==entry.index){
                    if(std::isfinite(upper[on]))entry.target=std::min(entry.target,nominal(upper[on]));
                    entry.target=std::max({tick,entry.target,lastOn[key],lastAttack,nominal(lower[on])});
                    jitter[on]=(static_cast<double>(entry.target)-static_cast<double>(entry.nominal))/frequency*1000;lastOn[key]=entry.target;lastAttack=entry.target;
                }else{entry.target=std::max({tick,entry.target,lastOff[key]});lastOff[key]=entry.target;}
            }
            entry.seconds=position+(static_cast<double>(entry.target)-static_cast<double>(tick))/frequency*speed;
        }
        std::make_heap(future.begin(),future.end(),Later{});seenSpeed=speed;
    }
    template<class Rows> void fill(const Rows& rows,const std::vector<unsigned char>& bytes,double position,double speed,unsigned amount,bool preserve,unsigned long long tick,unsigned long long frequency){
        if(!active){active=true;cursor=frontier;}
        if(seenSpeed!=speed)reanchor(rows,bytes,position,speed,preserve,tick,frequency);
        seenAmount=amount;
        const double horizon=position+.3*speed;
        if(cursor>=rows.size()||rows[cursor].seconds>horizon){advance();return;}
        auto nominal=[&](double stamp){return static_cast<unsigned long long>(std::max(0.,static_cast<double>(tick)+(stamp-position)/speed*frequency));};
        std::map<unsigned,unsigned long long> lastOn,lastOff;unsigned long long lastAttack=0;
        for(const auto& ready:future){const auto& r=rows[ready.index];if(r.size!=3)continue;const auto* p=bytes.data()+r.offset;
            const unsigned key=(r.port*16+(p[0]&15u))*128+p[1];
            if(mate[ready.index]==ready.index){lastOn[key]=std::max(lastOn[key],ready.target);lastAttack=std::max(lastAttack,ready.target);}
            else if(mate[ready.index]!=UINT_MAX)lastOff[key]=std::max(lastOff[key],ready.target);}
        for(;cursor<rows.size()&&rows[cursor].seconds<=horizon;++cursor){
            if(played[cursor])continue;const auto& r=rows[cursor];Ready entry{r.seconds,cursor,nominal(r.seconds),nominal(r.seconds)};
            if(r.size==3&&mate[cursor]!=UINT_MAX){const auto on=mate[cursor];
                if(cancelled[on]){played[cursor]=1;continue;}
                const auto* p=bytes.data()+r.offset;const unsigned key=(r.port*16+(p[0]&15u))*128+p[1];
                if(on==cursor){
                    if(std::isnan(jitter[on])){
                        const unsigned anchor=preserve?group[on]:on;
                        if(preserve&&anchor!=UINT_MAX&&!std::isnan(jitter[anchor]))jitter[on]=jitter[anchor];
                        else{jitter[on]=static_cast<int>(draw(amount*2+1))-static_cast<int>(amount);
                            if(preserve&&anchor!=UINT_MAX){
                                double floor=std::max(groupLower[anchor]/speed*1000,(static_cast<double>(lastAttack)-static_cast<double>(entry.nominal))/frequency*1000),ceiling=groupUpper[anchor]/speed*1000;
                                // A whole group uses the strongest already queued
                                // same-key constraint before its first member fires.
                                for(unsigned j=on;j<rows.size()&&rows[j].seconds<=rows[on].seconds+.005+1e-12;++j)if(group[j]==anchor){
                                    const auto& member=rows[j];const auto* data=bytes.data()+member.offset;
                                    const unsigned address=(member.port*16+(data[0]&15u))*128+data[1];
                                    floor=std::max(floor,(static_cast<double>(std::max(tick,lastOn[address]))-static_cast<double>(nominal(member.seconds)))/frequency*1000);
                                }
                                jitter[on]=std::max(floor,std::min(ceiling,jitter[on]));jitter[anchor]=jitter[on];
                            }
                        }
                    }
                    entry.target=static_cast<unsigned long long>(std::max(0.,static_cast<double>(entry.nominal)+jitter[on]*frequency/1000));
                    if(std::isfinite(upper[on]))entry.target=std::min(entry.target,nominal(upper[on]));
                    entry.target=std::max({tick,entry.target,lastOn[key],lastAttack,nominal(lower[on])});
                    jitter[on]=(static_cast<double>(entry.target)-static_cast<double>(entry.nominal))/frequency*1000;lastOn[key]=entry.target;lastAttack=entry.target;
                }else{const double shift=std::isnan(jitter[on])?0:jitter[on];
                    entry.target=static_cast<unsigned long long>(std::max(0.,static_cast<double>(entry.nominal)+shift*frequency/1000));
                    entry.target=std::max({tick,entry.target,lastOff[key]});lastOff[key]=entry.target;}
            }
            entry.seconds=position+(static_cast<double>(entry.target)-static_cast<double>(tick))/frequency*speed;
            future.push_back(entry);std::push_heap(future.begin(),future.end(),Later{});
        }
        advance();
    }
    template<class Rows> void cancelPast(const Rows& rows,const std::vector<unsigned char>& bytes,double position,unsigned channel){
        if(!active)return;
        for(const auto& entry:future){const auto& row=rows[entry.index];
            if(mate[entry.index]!=entry.index||row.seconds>position||row.size!=3)continue;
            if(channel==UINT_MAX||row.port*16+(bytes[row.offset]&15u)==channel){cancelled[entry.index]=1;played[entry.index]=1;}}
        future.erase(std::remove_if(future.begin(),future.end(),[&](const Ready& entry){
            if(played[entry.index]||(mate[entry.index]!=UINT_MAX&&cancelled[mate[entry.index]])){played[entry.index]=1;return true;}return false;}),future.end());
        std::make_heap(future.begin(),future.end(),Later{});advance();
    }
    void advance(){while(frontier<played.size()&&played[frontier])++frontier;}
    bool ready(unsigned long long tick)const{return !future.empty()&&future.front().target<=tick;}
    Ready peek()const{return future.front();}
    void commit(){auto row=future.front();std::pop_heap(future.begin(),future.end(),Later{});future.pop_back();played[row.index]=1;history.push_back(row);advance();}
    void skip(){auto row=future.front();std::pop_heap(future.begin(),future.end(),Later{});future.pop_back();played[row.index]=1;if(mate[row.index]==row.index)cancelled[row.index]=1;advance();}
    template<class Rows> void pause(const Rows& rows,double position){
        if(!active)return;
        for(unsigned i=frontier;i<cursor;++i)if(!played[i]&&rows[i].seconds<position){played[i]=1;if(mate[i]==i)cancelled[i]=1;}
        advance();future.clear();cursor=frontier;seenAmount=UINT_MAX;
    }
};
