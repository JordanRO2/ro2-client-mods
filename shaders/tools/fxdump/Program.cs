using System;using System.IO;using System.Linq;
using DXDecompiler.DX9Shader;using DXDecompiler.DX9Shader.FX9;using DXDecompiler.Util;
class P{static void Main(string[] a){foreach(var f in a){byte[] d=File.ReadAllBytes(f);
 EffectContainer e; try{e=EffectContainer.Parse(new BytecodeReader(d,4,d.Length-4),(uint)(d.Length-4));}catch{continue;}
 foreach(var v in e.Variables){ if(v.DefaultValue==null||v.DefaultValue.Count==0) continue;
  Console.WriteLine($"{Path.GetFileName(f)}|{v.Parameter.Name}|{string.Join(",",v.DefaultValue.Select(x=>x.Float.ToString("0.####")))}");}
 foreach(var t in e.Techniques) Console.WriteLine($"TECH|{Path.GetFileName(f)}|{t.Name}|{t.Passes.Count}");}}}
