// Headless Ghidra post-script: static program facts of the analysed binary as sorted JSON
// (schema: coordinator_core/contract/ghidra-program-facts.schema.json). Never executes the binary.
// Usage: analyzeHeadless ... -postScript ExportProgramFacts.java <output.json>

import ghidra.app.script.GhidraScript;
import ghidra.framework.Application;
import ghidra.program.model.address.Address;
import ghidra.program.model.data.StringDataInstance;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;
import ghidra.program.model.listing.Program;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.ReferenceManager;
import ghidra.program.model.symbol.Symbol;
import ghidra.program.model.symbol.SymbolIterator;
import ghidra.program.model.address.AddressIterator;
import ghidra.program.util.DefinedStringIterator;

import java.io.File;
import java.io.IOException;
import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

public class ExportProgramFacts extends GhidraScript {

    /** Upper bound on exported strings, applied after sorting so truncation is deterministic. */
    private static final int MAX_STRINGS = 50000;
    /** Upper bound on one exported string's characters. */
    private static final int MAX_STRING_CHARS = 4096;

    private static final String SCHEMA_VERSION = "ghidra-program-facts/v1";

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        if (args == null || args.length < 1) {
            throw new IllegalArgumentException("usage: ExportProgramFacts.java <output.json>");
        }
        File out = new File(args[0]);
        Program program = currentProgram;

        List<String[]> functions = collectFunctions(program);
        List<String[]> xrefs = collectXrefs(program);
        List<String[]> imports = collectImports(program);
        List<String[]> strings = collectStrings(program);

        functions.sort(keyOrder(0));
        xrefs.sort(keyOrder(0, 1, 2));
        imports.sort(keyOrder(0, 1, 2));
        strings.sort(keyOrder(0, 2));
        if (strings.size() > MAX_STRINGS) {
            strings = new ArrayList<>(strings.subList(0, MAX_STRINGS));
        }

        StringBuilder sb = new StringBuilder(1 << 20);
        sb.append("{\n");
        sb.append("  \"schema_version\": ").append(q(SCHEMA_VERSION)).append(",\n");
        sb.append("  \"program\": {\n");
        sb.append("    \"sha256\": ").append(q(nz(program.getExecutableSHA256()).toLowerCase())).append(",\n");
        sb.append("    \"name\": ").append(q(program.getName())).append(",\n");
        sb.append("    \"format\": ").append(q(nz(program.getExecutableFormat()))).append(",\n");
        sb.append("    \"language_id\": ").append(q(program.getLanguageID().getIdAsString())).append(",\n");
        sb.append("    \"image_base\": ").append(q(hex(program.getImageBase()))).append(",\n");
        sb.append("    \"ghidra_version\": ").append(q(Application.getApplicationVersion())).append("\n");
        sb.append("  },\n");

        sb.append("  \"functions\": [");
        emit(sb, functions, new String[] {"entry", "name", "size", "is_thunk", "is_external"},
            new boolean[] {false, false, true, true, true});
        sb.append("],\n  \"xrefs\": [");
        emit(sb, xrefs, new String[] {"from", "to", "ref_type"}, new boolean[] {false, false, false});
        sb.append("],\n  \"imports\": [");
        emit(sb, imports, new String[] {"library", "symbol", "address"}, new boolean[] {false, false, false});
        sb.append("],\n  \"strings\": [");
        emit(sb, strings, new String[] {"address", "value", "encoding"}, new boolean[] {false, false, false});
        sb.append("]\n}\n");

        File parent = out.getAbsoluteFile().getParentFile();
        if (parent != null) {
            Files.createDirectories(parent.toPath());
        }
        try (Writer w = Files.newBufferedWriter(out.toPath(), StandardCharsets.UTF_8)) {
            w.write(sb.toString());
        } catch (IOException e) {
            throw new IOException("cannot write " + out + ": " + e.getMessage(), e);
        }
        println("ExportProgramFacts: wrote " + out + " (functions=" + functions.size()
            + " xrefs=" + xrefs.size() + " imports=" + imports.size() + " strings=" + strings.size() + ")");
    }

    private List<String[]> collectFunctions(Program program) {
        List<String[]> rows = new ArrayList<>();
        FunctionIterator it = program.getFunctionManager().getFunctions(true);
        while (it.hasNext()) {
            rows.add(functionRow(it.next()));
        }
        FunctionIterator ext = program.getFunctionManager().getExternalFunctions();
        while (ext.hasNext()) {
            rows.add(functionRow(ext.next()));
        }
        return rows;
    }

    private String[] functionRow(Function f) {
        return new String[] {
            hex(f.getEntryPoint()),
            f.getName(),
            Long.toString(f.getBody().getNumAddresses()),
            Boolean.toString(f.isThunk()),
            Boolean.toString(f.isExternal())
        };
    }

    private List<String[]> collectXrefs(Program program) {
        List<String[]> rows = new ArrayList<>();
        ReferenceManager rm = program.getReferenceManager();
        AddressIterator sources = rm.getReferenceSourceIterator(program.getMemory(), true);
        while (sources.hasNext()) {
            Address from = sources.next();
            for (Reference ref : rm.getReferencesFrom(from)) {
                if (!ref.isMemoryReference() && !ref.isExternalReference()) {
                    continue;
                }
                rows.add(new String[] {hex(ref.getFromAddress()), hex(ref.getToAddress()),
                    ref.getReferenceType().getName()});
            }
        }
        return rows;
    }

    private List<String[]> collectImports(Program program) {
        List<String[]> rows = new ArrayList<>();
        SymbolIterator it = program.getSymbolTable().getExternalSymbols();
        while (it.hasNext()) {
            Symbol s = it.next();
            if (s.getName() == null || s.getName().isEmpty()) {
                continue;
            }
            String lib = s.getParentNamespace() == null ? "" : s.getParentNamespace().getName();
            if ("<EXTERNAL>".equals(lib)) {
                lib = "";
            }
            rows.add(new String[] {lib, s.getName(), hex(s.getAddress())});
        }
        return rows;
    }

    private List<String[]> collectStrings(Program program) {
        List<String[]> rows = new ArrayList<>();
        for (Data d : DefinedStringIterator.forProgram(program)) {
            StringDataInstance sdi = StringDataInstance.getStringDataInstance(d);
            String v = sdi.getStringValue();
            if (v == null) {
                continue;
            }
            if (v.length() > MAX_STRING_CHARS) {
                v = v.substring(0, MAX_STRING_CHARS);
            }
            rows.add(new String[] {hex(d.getAddress()), v, d.getDataType().getName()});
        }
        return rows;
    }

    private static Comparator<String[]> keyOrder(int... cols) {
        return (a, b) -> {
            for (int c : cols) {
                int r = a[c].compareTo(b[c]);
                if (r != 0) {
                    return r;
                }
            }
            return 0;
        };
    }

    private static void emit(StringBuilder sb, List<String[]> rows, String[] names, boolean[] raw) {
        for (int i = 0; i < rows.size(); i++) {
            sb.append(i == 0 ? "\n    {" : ",\n    {");
            String[] row = rows.get(i);
            for (int j = 0; j < names.length; j++) {
                if (j > 0) {
                    sb.append(", ");
                }
                sb.append(q(names[j])).append(": ").append(raw[j] ? row[j] : q(row[j]));
            }
            sb.append("}");
        }
        if (!rows.isEmpty()) {
            sb.append("\n  ");
        }
    }

    private static String hex(Address a) {
        return "0x" + Long.toHexString(a.getOffset());
    }

    private static String nz(String s) {
        return s == null ? "" : s;
    }

    private static String q(String s) {
        StringBuilder sb = new StringBuilder(s.length() + 2).append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                case '\b': sb.append("\\b"); break;
                case '\f': sb.append("\\f"); break;
                default:
                    if (c < 0x20 || c == 0x7f || (c >= 0xd800 && c <= 0xdfff
                            && !validSurrogate(s, i))) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        return sb.append('"').toString();
    }

    private static boolean validSurrogate(String s, int i) {
        char c = s.charAt(i);
        if (Character.isHighSurrogate(c)) {
            return i + 1 < s.length() && Character.isLowSurrogate(s.charAt(i + 1));
        }
        return i > 0 && Character.isHighSurrogate(s.charAt(i - 1));
    }
}
