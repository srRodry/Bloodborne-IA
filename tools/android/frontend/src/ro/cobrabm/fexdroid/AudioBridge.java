package ro.cobrabm.fexdroid;

import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioTrack;
import android.os.Process;
import android.os.SystemClock;
import android.system.Os;
import android.system.OsConstants;
import java.io.File;
import java.io.FileDescriptor;
import java.io.FileInputStream;
import java.io.RandomAccessFile;

/** Game audio: SDL's disk driver writes the mixed output (s16 stereo 48 kHz, run-thor.sh) into a
 *  FIFO; this plays it. The FIFO is held open read-write, so the game's open never blocks and a
 *  restarted game writes into the same pipe. The playback rate paces the game's audio thread. */
final class AudioBridge {
    private static final int RATE=48000, FRAME=4;
    private static final int F_SETPIPE_SZ=1031;
    private final File fifo;
    private volatile boolean running;
    private volatile AudioTrack track;

    AudioBridge(File fifo) { this.fifo=fifo; }

    void start() {
        if (running) return;
        running=true;
        Thread thread=new Thread(this::run,"Bloodborne-audio");
        thread.setPriority(Thread.MAX_PRIORITY); thread.setDaemon(true); thread.start();
    }
    void stop() { running=false; AudioTrack t=track; if (t!=null) t.pause(); }
    void pause() { AudioTrack t=track; if (t!=null) t.pause(); }
    void resume() { AudioTrack t=track; if (t!=null) t.play(); }

    private void run() {
        try {
            // Java MAX_PRIORITY does not map to Android's dedicated audio scheduling class.
            // Ask the platform for urgent-audio priority as well; failure is non-fatal.
            try { Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_AUDIO); }
            catch (Exception ignored) {}
            if (fifo.exists() && !isFifo(fifo)) fifo.delete();
            if (!fifo.exists()) Os.mkfifo(fifo.getPath(),0600);
            try (RandomAccessFile pipe=new RandomAccessFile(fifo,"rw");
                 FileInputStream in=new FileInputStream(pipe.getFD())) {
                FileDescriptor fd=pipe.getFD();
                // A small pipe keeps the delay short (~40 ms); the default 64 KiB is ~340 ms.
                try { Os.fcntlInt(fd,F_SETPIPE_SZ,8192); } catch (Exception ignored) {}
                int min=AudioTrack.getMinBufferSize(RATE,AudioFormat.CHANNEL_OUT_STEREO,AudioFormat.ENCODING_PCM_16BIT);
                AudioTrack t=new AudioTrack.Builder()
                    .setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_GAME)
                        .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build())
                    .setAudioFormat(new AudioFormat.Builder().setSampleRate(RATE)
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT).setChannelMask(AudioFormat.CHANNEL_OUT_STEREO).build())
                    .setBufferSizeInBytes(Math.max(min,RATE*FRAME/20))
                    .setTransferMode(AudioTrack.MODE_STREAM)
                    .setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY).build();
                track=t; t.play();
                byte[] buffer=new byte[4096];
                int carry=0, lastUnderruns=t.getUnderrunCount();
                long lastUnderrunCheck=SystemClock.uptimeMillis();
                while (running) {
                    int n=in.read(buffer,carry,buffer.length-carry);
                    if (n<=0) { Thread.sleep(5); continue; }
                    int whole=(carry+n)/FRAME*FRAME;
                    // AudioTrack may legally accept fewer bytes than requested even in stream mode.
                    // Dropping that remainder corrupts the PCM cadence under load, so drain every
                    // complete frame before reading the FIFO again.
                    int offset=0;
                    while (running && offset<whole) {
                        int wrote=t.write(buffer,offset,whole-offset,AudioTrack.WRITE_BLOCKING);
                        if (wrote<0) throw new java.io.IOException("AudioTrack.write failed: "+wrote);
                        if (wrote==0) { Thread.yield(); continue; }
                        offset+=wrote;
                    }
                    carry=carry+n-whole;
                    if (carry>0) System.arraycopy(buffer,whole,buffer,0,carry);
                    long now=SystemClock.uptimeMillis();
                    if (now-lastUnderrunCheck>=5000) {
                        int underruns=t.getUnderrunCount();
                        if (underruns>lastUnderruns)
                            android.util.Log.w("Bloodborne","audio underruns +"+(underruns-lastUnderruns)
                                +" (total "+underruns+")");
                        lastUnderruns=underruns; lastUnderrunCheck=now;
                    }
                }
                t.stop(); t.release(); track=null;
            }
        } catch (Exception ex) { android.util.Log.e("Bloodborne","audio: "+ex); }
    }
    private static boolean isFifo(File file) {
        try { return OsConstants.S_ISFIFO(Os.stat(file.getPath()).st_mode); } catch (Exception e) { return false; }
    }
}
