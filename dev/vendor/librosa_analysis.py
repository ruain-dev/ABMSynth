"""Small NumPy-only adaptation of librosa analysis primitives (ISC).

Copyright (c) 2013--2026, librosa development team.
Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.
THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

Pinned source and modifications: vendor/librosa-analysis/UPSTREAM.md.
This is NOT the full librosa package or its public API. Only mono, explicit
frames, max_size=1/lag=1/no-detrend onset strength and uniform-prior rhythm
autocorrelation are supported. No numba, SciPy, decoder, cache or JIT import.
Application acceptance/rejection policy belongs to instrument_creator.py.
"""
import numpy as np


def autocorrelate(y, maximum=None):
    """librosa.core.audio.autocorrelate, real one-dimensional FFT path."""
    size=1 << (2*len(y)-1).bit_length()
    spectrum=np.fft.rfft(y,n=size)
    return np.fft.irfft(spectrum.real**2+spectrum.imag**2,n=size)[:maximum or len(y)]


def yin_frames(frames,sr,fmin=20.,fmax=5000.,threshold=.1):
    """core.pitch.yin/CMND/parabolic interpolation; rows are explicit frames.

    Unlike upstream's global-min fallback, return the minimum depth alongside
    f0 so the caller can reject unvoiced noise. No input is modified.
    """
    length=frames.shape[1]
    low=max(1,int(np.floor(sr/min(fmax,sr/2))))
    high=min(int(np.ceil(sr/fmin)),length-1)
    fft_size=1 << (2*length-1).bit_length()
    spectrum=np.fft.rfft(frames,n=fft_size,axis=1)
    acf=np.fft.irfft(abs(spectrum)**2,n=fft_size,axis=1)[:,:high+1]
    cumulative=np.cumsum(frames**2,axis=1)
    cumulative[:,0]=0  # Preserve upstream's zero-lag assignment before RHS evaluation.
    difference=2*(acf[:,:1]-acf[:,1:])-cumulative[:,:high]
    denominator=np.cumsum(difference,axis=1)/np.arange(1,high+1)
    cmnd=difference[:,low-1:high]/(denominator[:,low-1:high]+np.finfo(float).tiny)
    trough=np.zeros(cmnd.shape,bool)
    trough[:,1:-1]=(cmnd[:,1:-1]<cmnd[:,:-2]) & (cmnd[:,1:-1]<=cmnd[:,2:])
    trough[:,0]=cmnd[:,0]<cmnd[:,1]
    accepted=trough & (cmnd<threshold)
    index=np.where(accepted.any(axis=1),accepted.argmax(axis=1),cmnd.argmin(axis=1))
    row=np.arange(len(frames));shift=np.zeros(len(frames))
    valid=(index>0)&(index<cmnd.shape[1]-1)
    r=row[valid];i=index[valid]
    a=cmnd[r,i+1]+cmnd[r,i-1]-2*cmnd[r,i]
    b=(cmnd[r,i+1]-cmnd[r,i-1])/2
    shift[valid]=np.divide(-b,a,out=np.zeros_like(b),where=abs(b)<abs(a))
    return sr/(low+index+shift),cmnd[row,index]


def mel_basis(sr,n_fft,n_mels=64):
    """filters.mel with Slaney piecewise mel spacing and area normalization."""
    def hz_to_mel(hz):
        hz=np.asarray(hz,float)
        return np.where(hz>=1000,15+np.log(np.maximum(hz,1)/1000)/(np.log(6.4)/27),hz/(200/3))
    def mel_to_hz(mel):
        return np.where(mel>=15,1000*np.exp((mel-15)*(np.log(6.4)/27)),mel*(200/3))
    mel_f=mel_to_hz(np.linspace(hz_to_mel(0),hz_to_mel(sr/2),n_mels+2))
    fftfreqs=np.fft.rfftfreq(n_fft,1/sr)
    fdiff=np.diff(mel_f);ramps=mel_f[:,None]-fftfreqs[None,:]
    lower=-ramps[:-2]/fdiff[:-1,None];upper=ramps[2:]/fdiff[1:,None]
    weights=np.maximum(0,np.minimum(lower,upper))
    return weights*(2/(mel_f[2:]-mel_f[:-2]))[:,None]


def onset_strength(log_mel):
    """onset.onset_strength_multi: lag=1,max_size=1,mean,center=False.

    Input is already log-power mel; compensation padding is one frame.
    """
    onset_env=log_mel[:,1:]-log_mel[:,:-1]
    onset_env=np.maximum(0.,onset_env)
    onset_env=np.mean(onset_env,axis=0)
    return np.pad(onset_env,(1,0))
