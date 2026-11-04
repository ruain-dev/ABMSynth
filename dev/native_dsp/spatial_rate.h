// Selectable-rate spatial-reverb boundary. BASS ABI: installed bass.h DSPPROC,
// STREAMPROC_DUMMY/GetData (float stereo). Preparation owns all allocations.
// 64-tap-base Kaiser8.6 polyphase sinc; downsample support grows with ratio.
// Original-rate dry has zero FIFO delay; wet has a fixed6ms alignment budget.
// DSPPROC never enters Python, creates resources, or allocates sample storage.
#pragma once

struct SpatialRateParameters {BASS_DX8_REVERB fx;float dry;};
static_assert(sizeof(SpatialRateParameters)==20,"spatial parameters ABI1");
struct SpatialRateSRC {
    static constexpr unsigned phases=512;
    unsigned half=0,taps=0,mask=0;double ratio=1;
    unsigned long long written=0;
    std::vector<float> coefficients,history;
    static double i0(double x){double sum=1,term=1;for(unsigned i=1;i<40;++i){term*=x*x/(4.*i*i);sum+=term;if(term<sum*1e-16)break;}return sum;}
    SpatialRateSRC(double source,double target):half(static_cast<unsigned>(std::ceil(32*std::max(1.,source/target)))),taps(2*half+1),ratio(source/target){
        // process() admits at most64 source frames; the largest up-history
        // push is ceil(64*48000/11025)=279 frames.512 covers this window
        // without the former unused2048-frame margin, including mirrored wrap.
        unsigned capacity=1;while(capacity<taps+512)capacity*=2;mask=capacity-1;
        // Mirror interleaved L/R history so each FIR window is contiguous.
        // SSE2 converts exactly the same binary32 samples to binary64; each
        // channel retains the original tap order and separate multiply/add.
        history.resize(static_cast<size_t>(capacity)*4);coefficients.resize(static_cast<size_t>(phases+1)*taps);
        const double cutoff=.47*std::min(1.,target/source),denominator=i0(8.6),pi=3.14159265358979323846;
        for(unsigned phase=0;phase<=phases;++phase){
            double sum=0;const double fraction=static_cast<double>(phase)/phases;
            for(unsigned k=0;k<taps;++k){const double distance=static_cast<double>(k)-half-fraction;
                const double normalized=distance/(half+1),window=i0(8.6*std::sqrt(std::max(0.,1-normalized*normalized)))/denominator;
                const double argument=2*pi*cutoff*distance;
                const double value=2*cutoff*(std::abs(argument)<1e-12?1:std::sin(argument)/argument)*window;
                coefficients[static_cast<size_t>(phase)*taps+k]=static_cast<float>(value);sum+=value;
            }
            for(unsigned k=0;k<taps;++k)coefficients[static_cast<size_t>(phase)*taps+k]=static_cast<float>(coefficients[static_cast<size_t>(phase)*taps+k]/sum);
        }
    }
    void reset(){written=0;std::fill(history.begin(),history.end(),0.f);}
    void push(const float* data,unsigned frames){for(unsigned i=0;i<frames;++i,++written){const auto at=static_cast<unsigned>(written)&mask;
        const auto pair=_mm_loadl_epi64(reinterpret_cast<const __m128i*>(data+2*i));
        _mm_storel_epi64(reinterpret_cast<__m128i*>(history.data()+2*at),pair);
        _mm_storel_epi64(reinterpret_cast<__m128i*>(history.data()+2*(at+mask+1)),pair);
    }}
    bool available(unsigned long long output)const{return static_cast<unsigned long long>(std::floor(output*ratio+1e-9))<written;}
    void sample(unsigned long long output,float* destination)const{
        const double position=output*ratio;const auto center=static_cast<long long>(std::floor(position+1e-9));
        const unsigned phase=std::min(phases,static_cast<unsigned>(std::max(0.,position-center)*phases+.5));
        const float* kernel=coefficients.data()+static_cast<size_t>(phase)*taps;
        __m128d sum=_mm_setzero_pd();
        const auto first=center-static_cast<long long>(2*half);
        const unsigned skip=first<0?static_cast<unsigned>(-first):0;
        const float* source=history.data()+2*(static_cast<unsigned>(first+skip)&mask);
        for(unsigned k=skip;k<taps;++k,source+=2){
            const auto samples=_mm_cvtps_pd(_mm_castsi128_ps(_mm_loadl_epi64(reinterpret_cast<const __m128i*>(source))));
            sum=_mm_add_pd(sum,_mm_mul_pd(samples,_mm_set1_pd(kernel[k])));
        }
        double values[2];_mm_storeu_pd(values,sum);destination[0]=static_cast<float>(values[0]);destination[1]=static_cast<float>(values[1]);
    }
    void samplePair(unsigned long long output,float* destination)const{
        const double position0=output*ratio,position1=(output+1)*ratio;
        const auto center0=static_cast<long long>(std::floor(position0+1e-9)),center1=static_cast<long long>(std::floor(position1+1e-9));
        const auto first0=center0-static_cast<long long>(2*half),first1=center1-static_cast<long long>(2*half);
        if(first0<0||first1<0){sample(output,destination);sample(output+1,destination+2);return;}
        const unsigned phase0=std::min(phases,static_cast<unsigned>(std::max(0.,position0-center0)*phases+.5));
        const unsigned phase1=std::min(phases,static_cast<unsigned>(std::max(0.,position1-center1)*phases+.5));
        const float* kernel0=coefficients.data()+static_cast<size_t>(phase0)*taps;
        const float* kernel1=coefficients.data()+static_cast<size_t>(phase1)*taps;
        const float* source0=history.data()+2*(static_cast<unsigned>(first0)&mask);
        const float* source1=history.data()+2*(static_cast<unsigned>(first1)&mask);
        __m128d sum0=_mm_setzero_pd(),sum1=_mm_setzero_pd();
        // Interleave independent output accumulators, never split/reassociate
        // one output's sum. This hides add latency without changing rounding.
        for(unsigned k=0;k<taps;++k,source0+=2,source1+=2){
            const auto samples0=_mm_cvtps_pd(_mm_castsi128_ps(_mm_loadl_epi64(reinterpret_cast<const __m128i*>(source0))));
            const auto samples1=_mm_cvtps_pd(_mm_castsi128_ps(_mm_loadl_epi64(reinterpret_cast<const __m128i*>(source1))));
            sum0=_mm_add_pd(sum0,_mm_mul_pd(samples0,_mm_set1_pd(kernel0[k])));
            sum1=_mm_add_pd(sum1,_mm_mul_pd(samples1,_mm_set1_pd(kernel1[k])));
        }
        double values0[2],values1[2];_mm_storeu_pd(values0,sum0);_mm_storeu_pd(values1,sum1);
        destination[0]=static_cast<float>(values0[0]);destination[1]=static_cast<float>(values0[1]);
        destination[2]=static_cast<float>(values1[0]);destination[3]=static_cast<float>(values1[1]);
    }
};
struct SpatialRateState {
    unsigned rate,processingRate,stream,fx;double padding;SpatialRateSRC down,up;
    DWORD(WINAPI* read)(DWORD,void*,DWORD);BOOL(WINAPI* set)(HFX,const void*);BOOL(WINAPI* freeStream)(HSTREAM);
    float dry=1;unsigned long long sourceFrames=0,workFrames=0;unsigned delayAt=0;
    std::vector<float> delay;
    std::atomic<unsigned> failures{0};
    BOOL(WINAPI* resetEffect)(HFX)=nullptr;
    SpatialRateState(unsigned frequency,unsigned workRate,unsigned channel,unsigned effect,void* getData,void* parameters,void* release):
        rate(frequency),processingRate(workRate),stream(channel),fx(effect),padding(frequency*.006-(frequency==workRate?0:std::ceil(32*std::max(1.,static_cast<double>(frequency)/workRate))+std::ceil(32*std::max(1.,static_cast<double>(workRate)/frequency))*frequency/workRate)),
        down(frequency,workRate),up(workRate,frequency),read(reinterpret_cast<DWORD(WINAPI*)(DWORD,void*,DWORD)>(getData)),
        set(reinterpret_cast<BOOL(WINAPI*)(HFX,const void*)>(parameters)),freeStream(reinterpret_cast<BOOL(WINAPI*)(HSTREAM)>(release)){
        delay.resize((static_cast<unsigned>(std::ceil(padding))+2)*2);
    }
    ~SpatialRateState(){if(stream)freeStream(stream);}
    bool reset(){down.reset();up.reset();sourceFrames=workFrames=delayAt=0;std::fill(delay.begin(),delay.end(),0.f);return !resetEffect||resetEffect(fx);}
    bool parameters(const SpatialRateParameters& value){if(!std::isfinite(value.dry)||!set(fx,&value.fx))return false;dry=value.dry;return true;}
    void process(float* data,unsigned frames){
        for(unsigned at=0;at<frames;at+=64){const unsigned count=std::min(64u,frames-at);float work[576],original[576];unsigned produced=0;
            if(rate==processingRate){produced=count;std::memcpy(work,data+2*at,count*8);}
            else{down.push(data+2*at,count);while(down.available(workFrames+1)){down.samplePair(workFrames,work+produced*2);workFrames+=2;produced+=2;}
                if(down.available(workFrames)){down.sample(workFrames++,work+produced*2);++produced;}}
            std::memcpy(original,work,produced*8);
            if(produced&&read(stream,work,produced*8)!=produced*8){++failures;for(unsigned i=0;i<produced*2;++i)work[i]=original[i]*dry;}
            for(unsigned i=0;i<produced*2;++i)work[i]-=original[i]*dry;
            if(rate!=processingRate)up.push(work,produced);
            float converted[128];
            if(rate!=processingRate){unsigned i=0;
                for(;i+1<count;i+=2){if(up.available(sourceFrames+i+1))up.samplePair(sourceFrames+i,converted+2*i);
                    else{for(unsigned j=i;j<i+2;++j){converted[2*j]=converted[2*j+1]=0;if(up.available(sourceFrames+j))up.sample(sourceFrames+j,converted+2*j);else ++failures;}}}
                if(i<count){converted[2*i]=converted[2*i+1]=0;if(up.available(sourceFrames+i))up.sample(sourceFrames+i,converted+2*i);else ++failures;}
            }
            const unsigned delayFrames=static_cast<unsigned>(delay.size()/2),whole=static_cast<unsigned>(padding);const float fraction=static_cast<float>(padding-whole);
            for(unsigned i=0;i<count;++i,++sourceFrames){float wet[2]{};
                if(rate==processingRate){wet[0]=work[2*i];wet[1]=work[2*i+1];}
                else{wet[0]=converted[2*i];wet[1]=converted[2*i+1];}
                const unsigned first=delayAt>=whole?delayAt-whole:delayAt+delayFrames-whole;
                const unsigned second=first?first-1:delayFrames-1;
                for(unsigned ch=0;ch<2;++ch){delay[2*delayAt+ch]=wet[ch];
                    data[2*(at+i)+ch]=data[2*(at+i)+ch]*dry+delay[2*first+ch]*(1-fraction)+delay[2*second+ch]*fraction;
                }
                if(++delayAt==delayFrames)delayAt=0;
            }
        }
    }
};
extern "C" __declspec(dllexport) unsigned __cdecl spatial_rate_version(){return 1;}
extern "C" __declspec(dllexport) void* __cdecl spatial_rate_create(unsigned rate,unsigned stream,unsigned fx,void* read,void* set,void* release){
    // Product range extends through768k;1536k is the existing independent DSP
    // witness. Minimum11025 ensures SRC support fits this fixed6ms wet budget.
    if(rate<11025||rate>1536000||!stream||!fx||!read||!set||!release)return nullptr;
    try{return new SpatialRateState(rate,48000,stream,fx,read,set,release);}catch(...){return nullptr;}
}
// Additive ABI: old fixed48k callers remain byte-compatible. New callers never
// upsample the private effect above its parent. Minimum11025 is a product/SRC
// alignment constraint, not a claimed BASS/DX8 maximum or minimum.
extern "C" __declspec(dllexport) void* __cdecl spatial_rate_create_ex(unsigned rate,unsigned workRate,unsigned stream,unsigned fx,void* read,void* set,void* release){
    if(rate<11025||rate>1536000||workRate<11025||workRate>rate||!stream||!fx||!read||!set||!release)return nullptr;
    try{return new SpatialRateState(rate,workRate,stream,fx,read,set,release);}catch(...){return nullptr;}
}
extern "C" __declspec(dllexport) unsigned __cdecl spatial_rate_processing_rate(void* pointer){return pointer?static_cast<SpatialRateState*>(pointer)->processingRate:0;}
extern "C" __declspec(dllexport) unsigned __cdecl spatial_rate_effect(void* pointer){return pointer?static_cast<SpatialRateState*>(pointer)->fx:0;}
extern "C" __declspec(dllexport) void __cdecl spatial_rate_destroy(void* pointer){delete static_cast<SpatialRateState*>(pointer);}
extern "C" __declspec(dllexport) int __cdecl spatial_rate_parameters(void* pointer,const SpatialRateParameters* value){return pointer&&value&&static_cast<SpatialRateState*>(pointer)->parameters(*value);}
extern "C" __declspec(dllexport) unsigned __cdecl spatial_rate_failures(void* pointer){return pointer?static_cast<SpatialRateState*>(pointer)->failures.load():0;}
extern "C" __declspec(dllexport) void __cdecl spatial_rate_reset_function(void* pointer,void* reset){if(pointer)static_cast<SpatialRateState*>(pointer)->resetEffect=reinterpret_cast<BOOL(WINAPI*)(HFX)>(reset);}
extern "C" __declspec(dllexport) void __cdecl spatial_rate_configure(unsigned stream,void* pointer,const SpatialRateParameters* value,unsigned reset,void* lockFunction,void* errorFunction,int* output){
    if(!output)return;std::fill(output,output+4,0);
    auto lock=reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(lockFunction);auto error=reinterpret_cast<int(WINAPI*)()>(errorFunction);
    if(!pointer||!value||!lock||!error){output[3]=2;return;}
    if(!lock(stream,TRUE)){output[1]=error();output[3]=1;return;}
    auto& state=*static_cast<SpatialRateState*>(pointer);
    if((reset&&!state.reset())||!state.parameters(*value)){output[1]=error();output[3]=2;}else output[0]=1;
    if(!lock(stream,FALSE)){output[2]=error();output[3]=3;}
}
extern "C" __declspec(dllexport) void CALLBACK spatial_rate_bass_dsp(DWORD,DWORD,void* buffer,DWORD bytes,void* user){
    if(buffer&&user)static_cast<SpatialRateState*>(user)->process(static_cast<float*>(buffer),bytes/8);
}
