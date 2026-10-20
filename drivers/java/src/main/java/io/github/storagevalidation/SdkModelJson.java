package io.github.storagevalidation;

import com.fasterxml.jackson.databind.JsonNode;
import java.time.Instant;
import java.util.*;
import software.amazon.awssdk.core.*;
import software.amazon.awssdk.core.protocol.MarshallingType;
import software.amazon.awssdk.core.protocol.MarshallLocation;
import software.amazon.awssdk.core.traits.ListTrait;
import software.amazon.awssdk.core.traits.MapTrait;
import software.amazon.awssdk.core.util.SdkAutoConstructList;
import software.amazon.awssdk.core.util.SdkAutoConstructMap;
import software.amazon.awssdk.utils.builder.Buildable;

/**
 * Avoids hand-written JSON conversions for every nested compatibility-request model.
 * Uses the SDK's public field descriptors and builders, rejects unknown input fields,
 * and reads actual SDK output getters while retaining uninitialized collection absence.
 */
final class SdkModelJson {
    static SdkPojo read(SdkPojo builder,JsonNode input) {
        if(!input.isObject()) throw new IllegalArgumentException("SDK structure must be a JSON object");
        Map<String,SdkField<?>> fields=new LinkedHashMap<>();
        for(SdkField<?> field:builder.sdkFields()) fields.put(field.memberName(),field);
        input.fields().forEachRemaining(entry->{
            SdkField<?> field=fields.get(entry.getKey());
            if(field==null) throw new UnsupportedOperationException("SDK model has no member "+entry.getKey());
            field.set(builder,readValue(field,entry.getValue()));
        });
        return (SdkPojo)((Buildable)builder).build();
    }

    private static Object readValue(SdkField<?> field,JsonNode value) {
        if(value.isNull())return null;
        MarshallingType<?> type=field.marshallingType();
        if(type==MarshallingType.SDK_POJO)return read(field.constructor().get(),value);
        if(type==MarshallingType.LIST) {
            if(!value.isArray())throw new IllegalArgumentException("SDK list must be a JSON array: "+field.memberName());
            List<Object> values=new ArrayList<>();SdkField<?> member=field.getRequiredTrait(ListTrait.class).memberFieldInfo();
            for(JsonNode item:value)values.add(readValue(member,item));return values;
        }
        if(type==MarshallingType.MAP) {
            if(!value.isObject())throw new IllegalArgumentException("SDK map must be a JSON object: "+field.memberName());
            Map<String,Object> values=new LinkedHashMap<>();SdkField<?> member=field.getRequiredTrait(MapTrait.class).valueFieldInfo();
            value.fields().forEachRemaining(entry->values.put(entry.getKey(),readValue(member,entry.getValue())));return values;
        }
        if(type==MarshallingType.STRING) {
            if(!value.isTextual())throw new IllegalArgumentException("SDK string must be textual: "+field.memberName());
            return value.textValue();
        }
        if(type==MarshallingType.BOOLEAN) {
            if(!value.isBoolean())throw new IllegalArgumentException("SDK boolean must be boolean: "+field.memberName());
            return value.booleanValue();
        }
        if(type==MarshallingType.INTEGER) {
            if(!value.canConvertToInt() || !value.isIntegralNumber())throw new IllegalArgumentException("SDK integer outside range: "+field.memberName());
            return value.intValue();
        }
        if(type==MarshallingType.LONG) {
            if(!value.canConvertToLong() || !value.isIntegralNumber())throw new IllegalArgumentException("SDK long outside range: "+field.memberName());
            return value.longValue();
        }
        if(type==MarshallingType.FLOAT)return value.floatValue();
        if(type==MarshallingType.DOUBLE)return value.doubleValue();
        if(type==MarshallingType.BIG_DECIMAL)return value.decimalValue();
        if(type==MarshallingType.INSTANT)return value.isIntegralNumber()?Instant.ofEpochMilli(value.longValue()):Instant.parse(value.textValue());
        if(type==MarshallingType.SDK_BYTES)return SdkBytes.fromByteArray(Base64.getDecoder().decode(value.textValue()));
        throw new UnsupportedOperationException("SDK field type unsupported: "+field.memberName()+" "+type);
    }

    static Map<String,Object> write(SdkPojo value) {
        Map<String,Object> result=new LinkedHashMap<>();
        for(SdkField<?> field:value.sdkFields()) {
            Object member=field.getValueOrDefault(value);
            if(member==null || member instanceof SdkAutoConstructList)continue;
            if(member instanceof SdkAutoConstructMap && field.location()!=MarshallLocation.HEADER)continue;
            result.put(field.memberName(),writeValue(member));
        }
        return result;
    }

    private static Object writeValue(Object value) {
        if(value instanceof SdkPojo)return write((SdkPojo)value);
        if(value instanceof Instant)return ((Instant)value).toEpochMilli();
        if(value instanceof SdkBytes)return Base64.getEncoder().encodeToString(((SdkBytes)value).asByteArray());
        if(value instanceof List<?>) {
            List<Object> result=new ArrayList<>();for(Object member:(List<?>)value)result.add(writeValue(member));return result;
        }
        if(value instanceof Map<?,?>) {
            Map<String,Object> result=new LinkedHashMap<>();((Map<?,?>)value).forEach((key,member)->result.put((String)key,writeValue(member)));return result;
        }
        return value;
    }
}
