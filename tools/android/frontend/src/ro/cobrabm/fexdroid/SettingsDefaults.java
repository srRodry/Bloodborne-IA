package ro.cobrabm.fexdroid;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.io.Writer;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** The Android default settings (bbport.default.ini, from tools/android/thor.ini) applied to bbport.ini
 *  by revision: on a first install (no bbport.ini), and once when updating from before the revision
 *  (a bbport.ini and no settings_defaults marker). Later updates find the marker and leave the player's
 *  settings alone. The marker holds the revision number; a revision above the stored one applies the
 *  defaults once more (raise REVISION only for that). Runs after RuntimeInstaller extracted the runtime
 *  (that is what brings the defaults file) and before run-thor.sh reads bbport.ini.
 *  When updating, every key of the defaults file takes its default value, except the player's own: the
 *  controller mapping (pad.*), the GPU driver, the mods menu position and language, and the mods switch.
 *  Keys that are not in the defaults file stay as they are, as do the comments. */
final class SettingsDefaults {
    static final int REVISION=2;
    private static final String MARKER="settings_defaults";
    private static final String[] KEPT={"gpu_driver","menu_pos","menu_language","mods_enabled"};

    private SettingsDefaults() {}

    private static boolean kept(String key) {
        if (key.startsWith("pad.")) return true;
        for (String k:KEPT) if (k.equals(key)) return true;
        return false;
    }
    /** The key of a "key=value" line (not a comment), else null; as SettingsDialog reads the file. */
    private static String keyOf(String line) {
        int eq=line.indexOf('=');
        return eq>0 && !line.startsWith("#") ? line.substring(0,eq).trim() : null;
    }
    private static List<String> lines(File file) throws IOException {
        List<String> out=new ArrayList<>();
        try (BufferedReader r=new BufferedReader(new InputStreamReader(new FileInputStream(file),"UTF-8"))) {
            for (String line; (line=r.readLine())!=null;) out.add(line);
        }
        return out;
    }
    /** key=value pairs, the last of a key winning (as run-thor.sh's setting()). */
    private static Map<String,String> values(List<String> lines) {
        Map<String,String> out=new LinkedHashMap<>();
        for (String line:lines) {
            String key=keyOf(line);
            if (key!=null) out.put(key,line.substring(line.indexOf('=')+1).trim());
        }
        return out;
    }
    /** Written whole beside the file, then renamed over it. */
    private static void write(File file,List<String> lines) throws IOException {
        File temporary=new File(file.getPath()+".tmp");
        try (FileOutputStream stream=new FileOutputStream(temporary);
             Writer w=new OutputStreamWriter(stream,"UTF-8")) {
            for (String line:lines) w.write(line+"\n");
            w.flush(); stream.getFD().sync();
        }
        if (!temporary.renameTo(file)) { temporary.delete(); throw new IOException("Cannot write "+file); }
    }
    private static int revision(File marker) {
        try {
            List<String> lines=lines(marker);
            return lines.isEmpty() ? 0 : Integer.parseInt(lines.get(0).trim());
        } catch (Exception e) { return 0; }
    }

    /** Applies the defaults in base (the bbport directory) when this revision has not been yet. Does
     *  nothing, and writes no marker, while the defaults file is missing. Throws when the files cannot
     *  be written; the marker is last, so the next start tries again. */
    static void apply(File base) throws IOException {
        File marker=new File(base,MARKER), ini=new File(base,"bbport.ini"), defaults=new File(base,"bbport.default.ini");
        if (revision(marker)>=REVISION || !defaults.isFile()) return;
        List<String> wantedLines=lines(defaults);
        Map<String,String> wanted=values(wantedLines);
        if (wanted.isEmpty()) return;
        boolean first=!ini.isFile();
        List<String> out;
        int changed=0;
        if (first) out=wantedLines; // what run-thor.sh would copy
        else {
            List<String> current=lines(ini);
            Map<String,String> before=values(current);
            out=new ArrayList<>(); Set<String> seen=new HashSet<>();
            for (String line:current) {
                String key=keyOf(line);
                if (key != null && wanted.containsKey(key)) {
                    seen.add(key);
                    if (!kept(key)) { // the last line wins in the file: every line of the key gets the default
                        out.add(key+"="+wanted.get(key));
                        if (!wanted.get(key).equals(before.get(key))) ++changed;
                        continue;
                    }
                }
                out.add(line);
            }
            for (Map.Entry<String,String> e:wanted.entrySet())
                if (!seen.contains(e.getKey())) { out.add(e.getKey()+"="+e.getValue()); ++changed; }
        }
        write(ini,out);
        // The cheats follow cheats.state while the game runs (run-thor.sh writes it at start too).
        Map<String,String> now=values(out);
        List<String> state=new ArrayList<>();
        state.add(("1".equals(now.get("cheat_health")) ? "1" : "0")+("1".equals(now.get("cheat_items")) ? "1" : "0"));
        write(new File(base,"cheats.state"),state);
        List<String> mark=new ArrayList<>(); mark.add(String.valueOf(REVISION));
        write(marker,mark);
        android.util.Log.i("Bloodborne","default settings revision "+REVISION+" applied ("+(first ? "first install"
            : changed+" settings changed; controller mapping, GPU driver and mod preferences kept")+")");
    }
}
