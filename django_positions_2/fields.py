from django.db import models
from django.db.models.signals import post_delete, post_save, pre_delete
from django.utils.timezone import now

# define basestring for python 3
basestring = (str, bytes)


class PositionField(models.IntegerField):
    def __init__(self, verbose_name=None, name=None, default=-1, collection=None, parent_link=None, *args, **kwargs):
        if 'unique' in kwargs:
            raise TypeError("%s can't have a unique constraint." % self.__class__.__name__)

        super(PositionField, self).__init__(verbose_name, name, default=default, *args, **kwargs)

        if isinstance(collection, basestring):
            collection = (collection,)

        self.collection = collection
        self.parent_link = parent_link
        self._collection_changed = None

    def get_cache_name(self):
        return '_%s_cache' % self.name

    def contribute_to_class(self, cls, name, **kwargs):
        super(PositionField, self).contribute_to_class(cls, name, **kwargs)

        for constraint in cls._meta.unique_together:
            if self.name in constraint:
                raise TypeError("%s can't be part of a unique constraint." % self.__class__.__name__)

        self.auto_now_fields = []

        for field in cls._meta.fields:
            if getattr(field, 'auto_now', False):
                self.auto_now_fields.append(field)

        setattr(cls, self.name, self)

        pre_delete.connect(self.prepare_delete, sender=cls)
        post_delete.connect(self.update_on_delete, sender=cls)
        post_save.connect(self.update_on_save, sender=cls)

    def pre_save(self, model_instance, add):
        # NOTE: check if the node has been moved to another collection; if it has, delete it from the old collection.
        previous_instance = None
        collection_changed = False
        if not add and self.collection is not None:
            try:
                previous_instance = type(model_instance)._default_manager.get(pk=model_instance.pk)
                for field_name in self.collection:
                    field = model_instance._meta.get_field(field_name)
                    current_field_value = getattr(model_instance, field.attname)
                    previous_field_value = getattr(previous_instance, field.attname)
                    if previous_field_value != current_field_value:
                        collection_changed = True
                        break
            except models.ObjectDoesNotExist:
                add = True
        if not collection_changed:
            previous_instance = None

        self._collection_changed = collection_changed
        if collection_changed:
            self.remove_from_collection(previous_instance)

        cache_name = self.get_cache_name()
        current, updated = getattr(model_instance, cache_name)

        if collection_changed:
            current = None

        if add:
            if updated is None:
                updated = current
            current = None

        # existing instance, position not modified; no cleanup required
        if current is not None and updated is None:
            return current

        # if updated is still unknown set the object to the last position,
        # either it is a new object or collection has been changed
        if updated is None:
            updated = -1
        
        collection_count = self.get_collection(model_instance).count()
        if current is None:
            max_position = collection_count
        else:
            max_position = collection_count - 1
        min_position = 0

        # new instance; appended; no cleanup required on post_save
        if add and (updated == -1 or updated >= max_position):
            setattr(model_instance, cache_name, (max_position, None))
            return max_position

        if max_position >= updated >= min_position:
            # positive position; valid index
            position = updated
        elif updated > max_position:
            # positive position; invalid index
            position = max_position
        elif abs(updated) <= (max_position + 1):
            # negative position; valid index

            # Add 1 to max_position to make this behave like a negative list index.
            # -1 means the last position, not the last position minus 1

            position = max_position + 1 + updated
        else:
            # negative position; invalid index
            position = min_position

        # instance inserted; cleanup required on post_save
        setattr(model_instance, cache_name, (current, position))
        return position

    def __get__(self, instance, owner):
        if instance is None:
            raise AttributeError("%s must be accessed via instance." % self.name)
        current, updated = getattr(instance, self.get_cache_name())
        return current if updated is None else updated

    def __set__(self, instance, value):
        if instance is None:
            raise AttributeError("%s must be accessed via instance." % self.name)
        if value is None:
            value = self.default
        cache_name = self.get_cache_name()
        try:
            current, updated = getattr(instance, cache_name)
        except AttributeError:
            current, updated = value, None
        else:
            updated = value

        instance.__dict__[self.name] = value  # Django 1.10 fix for deferred fields
        setattr(instance, cache_name, (current, updated))

    def get_collection(self, instance):
        filters = {}
        if self.collection is not None:
            for field_name in self.collection:
                field = instance._meta.get_field(field_name)
                field_value = getattr(instance, field.attname)
                if field.null and field_value is None:
                    filters['%s__isnull' % field.name] = True
                else:
                    filters[field.name] = field_value
        model = type(instance)
        parent_link = self.parent_link
        if parent_link is not None:
            model = model._meta.get_field(parent_link).rel.to
        return model._default_manager.filter(**filters)

    def get_next_sibling(self, instance):
        """
        Returns the next sibling of this instance.
        """
        try:
            kwargs = {'%s__gt' % self.name: getattr(instance, self.get_cache_name())[0]}
            return self.get_collection(instance).filter(**kwargs)[0]
        except Exception:
            return None

    def remove_from_collection(self, instance):
        """
        Removes a positioned item from the collection.
        """
        queryset = self.get_collection(instance)
        current = getattr(instance, self.get_cache_name())[0]
        updates = {self.name: models.F(self.name) - 1}
        if self.auto_now_fields:
            right_now = now()
            for field in self.auto_now_fields:
                updates[field.name] = right_now
        queryset.filter(**{'%s__gt' % self.name: current}).update(**updates)

    def prepare_delete(self, sender, instance, **kwargs):
        next_sibling = self.get_next_sibling(instance)
        if next_sibling:
            setattr(instance, '_next_sibling_pk', next_sibling.pk)
        else:
            setattr(instance, '_next_sibling_pk', None)
        pass

    def update_on_delete(self, sender, instance, **kwargs):
        next_sibling_pk = getattr(instance, '_next_sibling_pk', None)
        if next_sibling_pk:
            try:
                next_sibling = type(instance)._default_manager.get(pk=next_sibling_pk)
            except Exception:
                next_sibling = None
            if next_sibling:
                queryset = self.get_collection(next_sibling)
                current = getattr(instance, self.get_cache_name())[0]
                updates = {self.name: models.F(self.name) - 1}
                if self.auto_now_fields:
                    right_now = now()
                    for field in self.auto_now_fields:
                        updates[field.name] = right_now
                queryset.filter(**{'%s__gt' % self.name: current}).update(**updates)
        setattr(instance, '_next_sibling_pk', None)

    def update_on_save(self, sender, instance, created, **kwargs):
        collection_changed = self._collection_changed
        self._collection_changed = None

        current, updated = getattr(instance, self.get_cache_name())

        if updated is None and not collection_changed:
            return None

        queryset = self.get_collection(instance).exclude(pk=instance.pk)

        updates = {}
        if self.auto_now_fields:
            right_now = now()
            for field in self.auto_now_fields:
                updates[field.name] = right_now

        if updated is None and created:
            updated = -1

        if created or collection_changed:
            # increment positions gte updated or node moved from another collection
            queryset = queryset.filter(**{'%s__gte' % self.name: updated})
            updates[self.name] = models.F(self.name) + 1
        elif updated > current:
            # decrement positions gt current and lte updated
            queryset = queryset.filter(**{'%s__gt' % self.name: current, '%s__lte' % self.name: updated})
            updates[self.name] = models.F(self.name) - 1
        else:
            # increment positions lt current and gte updated
            queryset = queryset.filter(**{'%s__lt' % self.name: current, '%s__gte' % self.name: updated})
            updates[self.name] = models.F(self.name) + 1

        queryset.update(**updates)
        setattr(instance, self.get_cache_name(), (updated, None))
